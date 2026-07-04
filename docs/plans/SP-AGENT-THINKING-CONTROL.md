# SP-AGENT-THINKING-CONTROL — Thread optional thinking config through /v1/agent and the agent_engine client

Add an optional `thinking` field to `AgentRequest` (same shape as `MessagesRequest.thinking`), copy it verbatim in `_translate_request` so the existing per-provider reasoning machinery takes over, and thread the same field through `ProxyGatewayClient.call` and `AgentLoop` so every turn of a loop — tool turns, wrap-up, and wrap-up retry — inherits one policy per loop. Absent field means today's behaviour (no thinking config on the translated request).

## Step 1 — Add optional thinking field to AgentRequest

- **Files**: `src/ferova/llm_proxy/api/models/agent_v1.py`, `tests/unit/test_agent_thinking_control.py`
- **Action**: Import `ThinkingConfig` from `.anthropic` in `src/ferova/llm_proxy/api/models/agent_v1.py` and add `thinking: ThinkingConfig | None = None` to `AgentRequest` (defaulted optional, backward-compatible per A2). Create `tests/unit/test_agent_thinking_control.py` with a model-level test that constructs an `AgentRequest` carrying `ThinkingConfig(type="enabled", budget_tokens=1024)` and asserts the field round-trips through `model_dump(exclude_none=True)`.
- **Commit**: `feat(agent_v1): add optional thinking field to AgentRequest`
- **Done when**: `pytest tests/unit/test_agent_thinking_control.py::test_agent_request_accepts_thinking_field` passes
- **Unit tests**: `tests/unit/test_agent_thinking_control.py::test_agent_request_accepts_thinking_field`

## Step 2 — Thread thinking through _translate_request

- **Files**: `src/ferova/llm_proxy/api/agent_dispatcher.py`, `tests/unit/test_agent_thinking_control.py`
- **Action**: In `src/ferova/llm_proxy/api/agent_dispatcher.py::_translate_request`, pass `thinking=request.thinking` into the `MessagesRequest(...)` constructor. Extend `tests/unit/test_agent_thinking_control.py` with three tests pinning AC1/AC2/AC3: (a) an enabled thinking config on `AgentRequest` produces a `MessagesRequest` carrying the identical config; (b) absent field → translated request's `thinking is None`; (c) `{"type": "disabled"}` survives translation intact.
- **Commit**: `feat(agent_dispatcher): forward AgentRequest.thinking to MessagesRequest`
- **Done when**: `pytest tests/unit/test_agent_thinking_control.py::test_thinking_field_reaches_the_translated_request tests/unit/test_agent_thinking_control.py::test_absent_thinking_field_translates_to_none tests/unit/test_agent_thinking_control.py::test_disabled_thinking_round_trips` passes
- **Unit tests**: `tests/unit/test_agent_thinking_control.py::test_thinking_field_reaches_the_translated_request`, `tests/unit/test_agent_thinking_control.py::test_absent_thinking_field_translates_to_none`, `tests/unit/test_agent_thinking_control.py::test_disabled_thinking_round_trips`

## Step 3 — Add thinking kwarg to ProxyGatewayClient.call

- **Files**: `src/ferova/agent_engine/adapters.py`, `tests/unit/test_agent_thinking_control.py`
- **Action**: In `src/ferova/agent_engine/adapters.py::ProxyGatewayClient.call`, add a keyword-only `thinking: ThinkingConfig | dict[str, Any] | None = None` parameter (defaulted so every existing caller keeps today's behaviour — no field sent). When provided, attach it to the built `AgentRequest` so it lands in the POST body. Extend `tests/unit/test_agent_thinking_control.py` with a test that monkeypatches `httpx.Client`, calls `ProxyGatewayClient.call(..., thinking={"type": "enabled", "budget_tokens": 1024})`, and asserts the recorded POST body carries the thinking object verbatim; a second call without the kwarg asserts the body omits the field.
- **Commit**: `feat(agent_engine): accept thinking kwarg in ProxyGatewayClient.call`
- **Done when**: `pytest tests/unit/test_agent_thinking_control.py::test_proxy_client_threads_thinking_to_body tests/unit/test_agent_thinking_control.py::test_proxy_client_omits_thinking_when_unset` passes
- **Unit tests**: `tests/unit/test_agent_thinking_control.py::test_proxy_client_threads_thinking_to_body`, `tests/unit/test_agent_thinking_control.py::test_proxy_client_omits_thinking_when_unset`

## Step 4 — Thread thinking through AgentLoop (one policy per loop)

- **Files**: `src/ferova/agent_engine/agent_loop.py`, `tests/unit/test_agent_thinking_control.py`, `tests/integration/test_agent_thinking_control.py`
- **Action**: In `src/ferova/agent_engine/agent_loop.py::AgentLoop.__init__`, accept `thinking: ThinkingConfig | dict[str, Any] | None = None` and store it as `self._thinking`. Pass `thinking=self._thinking` to every `self._client.call(...)` site: `run_oneshot` (line ~414), `_call_turn_with_retry` (line ~528), the budget-exhausted wrap-up call (line ~788), and the wrap-up retry after a markup leak (line ~807). Extend `tests/unit/test_agent_thinking_control.py` with AC4: construct an `AgentLoop` with a thinking config, run a tool-using loop against a scripted `ProxyGatewayClient` stub that records kwargs, and assert the thinking config appears on the tool turn AND on the wrap-up call (one policy per loop, not per turn). Create `tests/integration/test_agent_thinking_control.py` with an end-to-end test that exercises the full pipeline (AgentLoop → ProxyGatewayClient → /v1/agent → _translate_request → MessagesRequest) and asserts the thinking config survives the round trip with the same value at every layer.
- **Commit**: `feat(agent_loop): thread thinking config through every turn of the loop`
- **Done when**: `pytest tests/unit/test_agent_thinking_control.py::test_agent_loop_threads_thinking_to_every_turn tests/integration/test_agent_thinking_control.py passes and `pytest tests/unit/` exits 0 (AC5)
- **Unit tests**: `tests/unit/test_agent_thinking_control.py::test_agent_loop_threads_thinking_to_every_turn`

## Integration tests

- `tests/integration/test_agent_thinking_control.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-AGENT-THINKING-CONTROL",
  "title": "Thread optional thinking config through /v1/agent and the agent_engine client",
  "summary": "Add an optional `thinking` field to `AgentRequest` (same shape as `MessagesRequest.thinking`), copy it verbatim in `_translate_request` so the existing per-provider reasoning machinery takes over, and thread the same field through `ProxyGatewayClient.call` and `AgentLoop` so every turn of a loop — tool turns, wrap-up, and wrap-up retry — inherits one policy per loop. Absent field means today's behaviour (no thinking config on the translated request).",
  "steps": [
    {
      "index": 1,
      "title": "Add optional thinking field to AgentRequest",
      "files": [
        "src/ferova/llm_proxy/api/models/agent_v1.py",
        "tests/unit/test_agent_thinking_control.py"
      ],
      "action": "Import `ThinkingConfig` from `.anthropic` in `src/ferova/llm_proxy/api/models/agent_v1.py` and add `thinking: ThinkingConfig | None = None` to `AgentRequest` (defaulted optional, backward-compatible per A2). Create `tests/unit/test_agent_thinking_control.py` with a model-level test that constructs an `AgentRequest` carrying `ThinkingConfig(type=\"enabled\", budget_tokens=1024)` and asserts the field round-trips through `model_dump(exclude_none=True)`.",
      "commit_message": "feat(agent_v1): add optional thinking field to AgentRequest",
      "done_when": "`pytest tests/unit/test_agent_thinking_control.py::test_agent_request_accepts_thinking_field` passes",
      "unit_tests": [
        "tests/unit/test_agent_thinking_control.py::test_agent_request_accepts_thinking_field"
      ]
    },
    {
      "index": 2,
      "title": "Thread thinking through _translate_request",
      "files": [
        "src/ferova/llm_proxy/api/agent_dispatcher.py",
        "tests/unit/test_agent_thinking_control.py"
      ],
      "action": "In `src/ferova/llm_proxy/api/agent_dispatcher.py::_translate_request`, pass `thinking=request.thinking` into the `MessagesRequest(...)` constructor. Extend `tests/unit/test_agent_thinking_control.py` with three tests pinning AC1/AC2/AC3: (a) an enabled thinking config on `AgentRequest` produces a `MessagesRequest` carrying the identical config; (b) absent field → translated request's `thinking is None`; (c) `{\"type\": \"disabled\"}` survives translation intact.",
      "commit_message": "feat(agent_dispatcher): forward AgentRequest.thinking to MessagesRequest",
      "done_when": "`pytest tests/unit/test_agent_thinking_control.py::test_thinking_field_reaches_the_translated_request tests/unit/test_agent_thinking_control.py::test_absent_thinking_field_translates_to_none tests/unit/test_agent_thinking_control.py::test_disabled_thinking_round_trips` passes",
      "unit_tests": [
        "tests/unit/test_agent_thinking_control.py::test_thinking_field_reaches_the_translated_request",
        "tests/unit/test_agent_thinking_control.py::test_absent_thinking_field_translates_to_none",
        "tests/unit/test_agent_thinking_control.py::test_disabled_thinking_round_trips"
      ]
    },
    {
      "index": 3,
      "title": "Add thinking kwarg to ProxyGatewayClient.call",
      "files": [
        "src/ferova/agent_engine/adapters.py",
        "tests/unit/test_agent_thinking_control.py"
      ],
      "action": "In `src/ferova/agent_engine/adapters.py::ProxyGatewayClient.call`, add a keyword-only `thinking: ThinkingConfig | dict[str, Any] | None = None` parameter (defaulted so every existing caller keeps today's behaviour — no field sent). When provided, attach it to the built `AgentRequest` so it lands in the POST body. Extend `tests/unit/test_agent_thinking_control.py` with a test that monkeypatches `httpx.Client`, calls `ProxyGatewayClient.call(..., thinking={\"type\": \"enabled\", \"budget_tokens\": 1024})`, and asserts the recorded POST body carries the thinking object verbatim; a second call without the kwarg asserts the body omits the field.",
      "commit_message": "feat(agent_engine): accept thinking kwarg in ProxyGatewayClient.call",
      "done_when": "`pytest tests/unit/test_agent_thinking_control.py::test_proxy_client_threads_thinking_to_body tests/unit/test_agent_thinking_control.py::test_proxy_client_omits_thinking_when_unset` passes",
      "unit_tests": [
        "tests/unit/test_agent_thinking_control.py::test_proxy_client_threads_thinking_to_body",
        "tests/unit/test_agent_thinking_control.py::test_proxy_client_omits_thinking_when_unset"
      ]
    },
    {
      "index": 4,
      "title": "Thread thinking through AgentLoop (one policy per loop)",
      "files": [
        "src/ferova/agent_engine/agent_loop.py",
        "tests/unit/test_agent_thinking_control.py",
        "tests/integration/test_agent_thinking_control.py"
      ],
      "action": "In `src/ferova/agent_engine/agent_loop.py::AgentLoop.__init__`, accept `thinking: ThinkingConfig | dict[str, Any] | None = None` and store it as `self._thinking`. Pass `thinking=self._thinking` to every `self._client.call(...)` site: `run_oneshot` (line ~414), `_call_turn_with_retry` (line ~528), the budget-exhausted wrap-up call (line ~788), and the wrap-up retry after a markup leak (line ~807). Extend `tests/unit/test_agent_thinking_control.py` with AC4: construct an `AgentLoop` with a thinking config, run a tool-using loop against a scripted `ProxyGatewayClient` stub that records kwargs, and assert the thinking config appears on the tool turn AND on the wrap-up call (one policy per loop, not per turn). Create `tests/integration/test_agent_thinking_control.py` with an end-to-end test that exercises the full pipeline (AgentLoop → ProxyGatewayClient → /v1/agent → _translate_request → MessagesRequest) and asserts the thinking config survives the round trip with the same value at every layer.",
      "commit_message": "feat(agent_loop): thread thinking config through every turn of the loop",
      "done_when": "`pytest tests/unit/test_agent_thinking_control.py::test_agent_loop_threads_thinking_to_every_turn tests/integration/test_agent_thinking_control.py passes and `pytest tests/unit/` exits 0 (AC5)",
      "unit_tests": [
        "tests/unit/test_agent_thinking_control.py::test_agent_loop_threads_thinking_to_every_turn"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_agent_thinking_control.py"
  ]
}
```
