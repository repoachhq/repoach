# Thinking-path audit (SP-CHAINPILOT-THINKING-AUDIT, Phase 0a)

Findings of the audit that precedes un-banning thinking models
(`docs/chain_autopilot_architecture.md`, principle 4). Read-only; the
companion characterization tests
(`tests/unit/test_chainpilot_thinking_audit.py`) pin the behaviour described
here so Phase 0b/0c refactor against a green safety net.

## Headline

The machinery to cope with a reasoning model **already largely exists** —
the `chains.env` "only NO_THINKING models" rule predates it and is now more
conservative than the system requires. Two mechanisms already shipped:

1. **Budget-starvation detection** — `peek_for_content` (`api/_failover.py`)
   sets `looks_budget_starved=True` when a stream *completes normally* but
   yields `output_tokens == 0` or whitespace-only text, distinct from a
   transport/error empty (`stop_reason == "error"` → `looks_budget_starved=False`).
2. **Budget-retry** — the dispatcher (`api/services.py:283-292` →
   `_retry_with_more_budget`) re-issues the SAME budget-starved candidate
   once with `max_tokens` enlarged by `budget_retry_factor` (floored/capped)
   before failing over. A capable model that only needed headroom is kept.

So a thinking model that burns its budget is detected and retried, not
blindly failed over. The remaining gap is about *guaranteeing headroom*, not
detecting its absence.

## Gap matrix (per transport)

| Transport | Reasoning budget bounded? | Reasoning streamed | Gated by `enable_thinking` |
|-----------|---------------------------|--------------------|----------------------------|
| **NIM** (`nvidia_nim/request.py`) | **Yes** — `_bounded_reasoning_budget` caps `chat_template_kwargs.reasoning_budget` at 2048 (half of max_tokens, floor 256), so visible output always has headroom | `reasoning_content` deltas → thinking blocks (`openai_compat`) | yes (`_is_thinking_enabled`) |
| **GenericOpenAI** (kimi/groq/cerebras/deepseek, via `build_base_request_body`) | **No** — no reasoning-budget shaping at all; a thinking model here can spend the full (even enlarged) `max_tokens` on hidden reasoning | `reasoning_content` deltas → thinking blocks (shared `openai_compat`) | yes |
| **OpenRouter** (`open_router/request.py`) | Partial — maps `thinking.budget_tokens` → `reasoning.max_tokens` only when the *client* set it; no default bound | native Anthropic SSE; thinking blocks filtered per policy | yes |
| **claude_code** | N/A — subprocess, single JSON result, no streaming reasoning | n/a | n/a |

## The real gap → Phase 0b/0c scope

**0b — `SP-CHAINPILOT-THINKING-BUDGET`.** Guarantee visible-output headroom
on the transports that today bound nothing. Concretely: the GenericOpenAI
path (and OpenRouter's no-client-budget case) must apply a default reasoning
budget bound analogous to NIM's `_bounded_reasoning_budget`, so that even a
spontaneously-thinking model leaves room for the answer — and so the
budget-retry actually converges instead of re-burning the enlarged budget on
reasoning. Likely shape: lift `_bounded_reasoning_budget` to a shared helper
and apply it per transport's native budget knob.

**0c — `SP-CHAINPILOT-THINKING-PEEK`.** Smaller than the umbrella first
implied: `peek_for_content` *already* distinguishes "budget-starved
(retryable)" from "dead", and the retry path exists. 0c reduces to
**confirming and widening coverage** — assert the starved→retry→success path
end-to-end for a thinking-shaped stream, and verify the retry's enlarged
budget interacts correctly with the 0b headroom bound (the enlargement must
raise the answer ceiling, not the reasoning ceiling). No new failover logic
is anticipated; if the end-to-end test passes once 0b lands, 0c is a
coverage-only slice and may fold into 0b's PR.

## Conclusion

Un-banning thinking is **one real change** (0b: a shared headroom bound on
the unbounded transports) plus a **confirmation** (0c). The detection and
retry spine is already in place. This de-risks the rest of the arc: once 0b
lands, a thinking model is a normal chain candidate.
</content>
