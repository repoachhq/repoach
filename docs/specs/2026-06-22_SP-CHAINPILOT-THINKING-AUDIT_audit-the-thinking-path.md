---
id: SP-CHAINPILOT-THINKING-AUDIT
title: Audit the thinking path and pin its current behaviour
version: 0.1
status: approved
author: agent
created: 2026-06-22
updated: 2026-06-22

owns:
  code: [docs/chain_autopilot_thinking_audit.md]   # the audit findings doc (non-importable; import tier never fires)
  resources: N/A                                    # doc + characterization tests only; no shared state

depends_on: []                                      # reads existing code; introduces no governed coupling
provides_to: []                                     # AUTO-maintained

constraints: {}
---

# SP-CHAINPILOT-THINKING-AUDIT — audit the thinking path and pin its current behaviour

## Intent
Phase 0a of the Chain Autopilot arc
(`docs/chain_autopilot_architecture.md`). Before we un-ban thinking models
(principle 4), map exactly what the proxy *already* does with a reasoning
stream and what it does not, so the two follow-up slices (0b budget bound,
0c peek) are scoped against reality, not a guess. Deliver a characterization
test suite that pins today's behaviour as a safety net, plus a findings doc
that defines 0b/0c precisely.

## Context
The `chains.env` rule "only NO_THINKING models" predates real machinery
that already exists: `peek_for_content` (`api/_failover.py`) computes
`looks_budget_starved` (a normal-completion empty/whitespace stream, distinct
from a transport/error empty), and there is an SP-PROXY-THINKING-BUDGET-RETRY
path that retries a budget-starved candidate with a larger budget before
failing over (`api/services.py`). NIM bounds the reasoning budget
(`nvidia_nim/request.py:_bounded_reasoning_budget`, cap 2048); the OpenRouter
transport maps `thinking.budget_tokens` to `reasoning.max_tokens`; the
`GenericOpenAIProvider` (kimi/groq/cerebras/deepseek) goes through
`build_base_request_body` with no reasoning-budget shaping. The audit
establishes the true gap matrix across these touchpoints. No behaviour change.

## Goals
- G1: A characterization test suite (`tests/unit/`) pinning the CURRENT
  behaviour of the thinking path so 0b/0c can refactor against a green
  safety net — covering at minimum: `peek_for_content`'s
  `looks_budget_starved` decision on an output-tokens-zero / whitespace-only
  thinking stream; the budget-retry trigger; and each transport's
  reasoning-budget shaping (present vs absent).
- G2: A findings doc `docs/chain_autopilot_thinking_audit.md` recording, per
  transport (NIM, GenericOpenAI, OpenRouter, claude_code), whether the
  reasoning budget is bounded, how `reasoning_content` / thinking deltas are
  streamed and gated, and how a budget-starved stream is detected and
  retried — a gap matrix.
- G3: The doc states the precise scope of 0b (`THINKING-BUDGET` — which
  transports need a budget bound and where) and 0c (`THINKING-PEEK` —
  whether peek already distinguishes "still thinking" from "dead", and what,
  if anything, remains).

## Non-Goals
- NG1: Changes NO production behaviour — characterization tests must pass
  against the code as-is.
- NG2: Does NOT modify `chains.env` or add any thinking model to a chain.
- NG3: Does NOT implement the budget bound (0b) or peek changes (0c).

## Assumptions
- A1: The touchpoints named in Context are the complete thinking path; the
  audit confirms this by grep and records any additional site found.

## Interface
This is an audit slice — its interface is the findings doc schema, not a
function signature.

`docs/chain_autopilot_thinking_audit.md`:
- a per-transport gap matrix (budget-bounded? reasoning streamed? gated by
  `enable_thinking`?);
- the budget-starvation → retry flow as it exists today;
- the scoped definitions of 0b and 0c.

## Behavior

### Nominal
The characterization tests encode today's observable behaviour and pass on
the current tree. The doc reflects the code as read.

### Edge cases
- A transport with no reasoning-budget shaping (GenericOpenAI) is recorded
  as an explicit gap, not an omission.

### Failure scenarios
- N/A — read-only audit; no runtime path added.

## Architecture Impact
- Introduces the audit doc artifact; `depends_on: []`.
- New / changed coupling, cycles, or shared state: none.

## Diagram
N/A — audit slice; the flow under study is the existing failover path
(`docs/chain_autopilot_architecture.md` Phase 0).

## Acceptance Criteria
- [ ] AC1: `docs/chain_autopilot_thinking_audit.md` exists with a
  per-transport reasoning-budget/streaming/gating gap matrix.
- [ ] AC2: a characterization test pins `peek_for_content`'s
  `looks_budget_starved=True` on an output-tokens-zero (and on a
  whitespace-only) completed stream, and `False` on a `stop_reason=error`
  stream.
- [ ] AC3: a characterization test pins each transport's request body for a
  thinking-enabled request (NIM bounds `reasoning_budget`; GenericOpenAI
  does not), documenting the gap 0b will close.
- [ ] AC4: the doc states the scoped 0b and 0c definitions, each as a
  one-paragraph spec seed.
- [ ] AC5: the full unit suite stays green (no behaviour change).

## Open Questions
- None.
</content>
