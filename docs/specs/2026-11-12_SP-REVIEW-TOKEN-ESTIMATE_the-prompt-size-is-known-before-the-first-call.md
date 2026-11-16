---
id: SP-REVIEW-TOKEN-ESTIMATE
title: The size of a review is estimated before its first model call, with the one estimator the budget guard owns, and a cap refuses it by name
version: 0.2
status: draft
author: agent
created: 2026-11-12
updated: 2026-11-16

owns:
  code: [src/repoach/review/token_estimate.py, tests/unit/test_token_estimate.py]
  resources: []

depends_on: [SP-ANTHROPIC-BUDGET-GUARD, SP-USAGE-LEDGER, SP-REVIEW-FILE-SELECTION, SP-REFUTER-JUDGES-WHAT-IT-CAN, SP-UNIVERSAL-PROJECT-CONFIG, SP-CLI-STDOUT-ONE-JSON, SP-ORCH-DOCSTRING, SP-DB-PATH-XDG]
provides_to: []

constraints:
  token_estimator: SP-ANTHROPIC-BUDGET-GUARD estimate_prompt_tokens (one definition)
  max_prompt_tokens_default: 0
---

# The size of a review is estimated before its first model call

## Amendment (0.2, 2026-11-16): one estimator, owned by SP-ANTHROPIC-BUDGET-GUARD

The 0.1 D1 defined its own estimator, `ceil(utf8_bytes / 3)`, shared
with the refuter's evidence sizing. SP-ANTHROPIC-BUDGET-GUARD (2026-11-15)
needs a prompt-token estimate before every metered call to price it, and
its branch `feat/anthropic-budget-guard` already implements one
(`src/repoach/providers/budget.py::estimate_prompt_tokens`, the
`CHARS_PER_TOKEN = 3.2` fallback, beside the gateway's `tiktoken`
`cl100k_base` request count in `src/repoach/llm_proxy/core/anthropic/tokens.py`).
Two estimators would print two figures for one prompt. Decision: the
budget guard owns the estimator, because it is the one that pays for
being wrong (an under-estimate admits a call the budget cannot cover);
this spec imports it, aggregates it per role and enforces the cap, and
the refuter's `TOKEN_ESTIMATOR` string is re-exported from the budget
module so the name has one definition. D1, F3, the Interface, the tests
and the Architecture Impact change accordingly.

## Intent

A review's cost is known after it was paid: the usage ledger writes one
row per call. After this spec `review pr` estimates, before any model
call, the prompt tokens each role will send and the refuter's ceiling,
prints the estimate, compares it with the measured usage at the end,
and refuses by name a review whose estimate exceeds a cap the project
set. `review pr --estimate` prints the estimate and exits without a
call.

## Context

Measured on `origin/develop` `8fabb19` (2026-11-12); F6 on 2026-11-16.

| # | Fact | Where |
|---|---|---|
| F1 | Every provider call writes one `usage_ledger` row after the call; `review pr` sums them on stderr: `repoach: usage: 8 calls, … tokens, … s, <cost>` | `src/repoach/providers/usage.py`, README "Providers" |
| F2 | A Yoob review costs 224–300 k tokens, more than 95 % cached; the planted corpus 134 989 to 349 091 per case | README Status; `README.md` bench block |
| F3 | The refuter's evidence is sized with one disclosed estimator, `ceil(utf8_bytes / 3)`, against `[gates].refuter_context_tokens`; the judge count is bounded by `refuter_max_judged`. From 0.2 that string is the budget guard's estimator name, re-exported | `src/repoach/review/refuter_evidence.py:67`, `src/repoach/project/config.py:468-472` |
| F4 | The reviewers' prompt is the persona with the diff, the spec block, the excerpts and the lessons substituted; its size is known before `run_oneshot` is called | `src/repoach/review/reviewer.py:725-776` (SP-AGENT-CONTEXT-LOOP F1) |
| F5 | open-code-review prints an order-of-magnitude estimate before a large review and stops dispatching groups when `--max-tokens-budget` would be exceeded, exiting 0 with the partial result; the estimate is documented as a floor that cannot account for tool use | `R/internal/agent/estimate.go:11-21`, `agent.go:683-711` |
| F6 | The budget guard's branch estimates a prompt as `ceil(len(text) / 3.2)` when the route gives no count, an upper bound on code measured on its fixtures; the gateway counts a request with `tiktoken` `cl100k_base`; `tiktoken>=0.7` is a dependency on `develop` | `repoach-wt-budget-guard/src/repoach/providers/budget.py:72,324-326`; `src/repoach/llm_proxy/core/anthropic/tokens.py:1-12`; `pyproject.toml:53` |

## Decisions (author, within the owner's brief; D1 amended 2026-11-16)

- D1 (0.2). One estimator, SP-ANTHROPIC-BUDGET-GUARD's
  `estimate_prompt_tokens(text) -> int` with its `ESTIMATOR_NAME`, for
  every figure: `prompt_tokens(role) = estimate_prompt_tokens(prompt)` on
  the rendered prompt; `refuter_max = refuter_max_judged ×
  refuter_context_tokens`; the total is the four roles plus
  `refuter_max`. The figure is a disclosed estimate, named by
  `ESTIMATOR_NAME` wherever it is printed, and never called `cost`; the
  budget guard decides the rule (its `tiktoken` count when the text is at
  hand, else `ceil(len / 3.2)`), this spec never redefines it.
  `refuter_evidence.TOKEN_ESTIMATOR` becomes a re-export of
  `ESTIMATOR_NAME`.
- D2. Printed before the first call: stderr `estimate: architect 48 k,
  sentinel 48 k, tester 49 k, scribe 48 k prompt tokens; refuter at
  most 8 × 40 k; total at most 513 k (<ESTIMATOR_NAME>)` and the JSON
  `estimate: {roles: {...}, refuter_max, total_max, estimator}`. At the
  end the usage line gains `(estimate 513 k max, measured 231 k prompt)`.
- D3. `[review].max_prompt_tokens` (default 0, unbounded;
  `REPOACH_MAX_PROMPT_TOKENS` outranks it): when the four roles' sum
  exceeds it, `review pr` exits 2 before any forge write or model call
  with `repoach: estimate 310 k prompt tokens > [review].max_prompt_tokens
  250 k; the diff has 41 files (files: ...); exclude files, raise the
  cap, or pass --force`. The refuter's ceiling is not counted in the
  cap: it is bounded by its own two knobs (F3).
- D4. `review pr --estimate` renders the prompts, prints D2's lines and
  the JSON, writes nothing and calls no model; it is a dry run of the
  dry run, usable in a pre-push hook.
- D5. The estimate is written on the run's `review_runs` row
  (SP-UNIVERSAL-LEDGER G3) beside the measured tokens, so `repoach
  stats` can print the estimator's error over time (`estimate /
  measured`, median and max), which is also the budget guard's error.

## Goals

- G1: the estimate exists before the first model call, with the budget guard's estimator (D1), and is printed and carried in the JSON with the estimator's name (D2).
- G2: `max_prompt_tokens` refuses by name before any call or forge write (D3); `--force` passes.
- G3: `review pr --estimate` makes no model call and no forge write (D4).
- G4: the usage line compares estimate and measurement; the run row keeps both (D5).
- G5 (0.2): `grep -rn "utf8_bytes / 3\|/ 3.2\|cl100k" src/repoach` finds the rule in `providers/budget.py` only.

## Non-Goals

- NG1: no estimator of its own, no price in the estimate (the price is the budget guard's).
- NG2: no partial review under the cap (F5's look-ahead): repoach reviews the whole change or refuses by name; partial coverage is SP-REVIEW-GROUPS-LARGE-DIFFS's object, named there.
- NG3: no change to the refuter's own budgeting (F3) beyond the re-export.

## Assumptions

- A1: the four prompts are rendered before the executor fans out, so measuring them adds no model call and under 50 ms with the fallback rule, under 500 ms with `tiktoken` on a 300 k-character prompt (measured before the PR opens; the budget guard's tests pin the same bound).

## Interface

```python
# repoach.review.token_estimate
from repoach.providers.budget import ESTIMATOR_NAME, estimate_prompt_tokens   # the one estimator (SP-ANTHROPIC-BUDGET-GUARD)
class ReviewEstimate(BaseModel):
    roles: dict[str, int]; refuter_max: int; total_max: int; estimator: str   # estimator == ESTIMATOR_NAME
    def roles_total(self) -> int: ...
def estimate_review(prompts: Mapping[str, str], gates: GatesSection) -> ReviewEstimate: ...
class EstimateRefused(Exception): ...        # rc 2, message as D3
def enforce_cap(estimate: ReviewEstimate, cap: int, *, force: bool, files: Sequence[str]) -> None: ...

# repoach.review.refuter_evidence (changed): TOKEN_ESTIMATOR = ESTIMATOR_NAME (re-export), sizing calls estimate_prompt_tokens
# repoach.project.config.ReviewSection (added)
max_prompt_tokens: int = 0     # 0 = unbounded, else 10_000..5_000_000; REPOACH_MAX_PROMPT_TOKENS
```

CLI: `repoach review pr <N> --estimate`; the JSON gains `estimate`.

## Behavior

### Nominal
- A 12-file merge request: `estimate: … total at most 513 k (cl100k_base)`; the review runs; `usage: 9 calls, 231 k tokens … (estimate 513 k max, measured 231 k prompt)`.

### Edge cases
- `max_prompt_tokens = 250000`, estimate 193 k roles: runs. Estimate 310 k: rc 2 with the message; `--force`: runs and the JSON says `estimate.forced: true`.
- `--estimate --dry-run`: `--estimate` wins, nothing is called.

### Failure scenarios
- `REPOACH_MAX_PROMPT_TOKENS=abc`: rc 2 naming the variable, before any call.

## Architecture Impact
- Adds dependency (0.2): SP-REVIEW-TOKEN-ESTIMATE -> SP-ANTHROPIC-BUDGET-GUARD (the one estimator and its name; this spec aggregates, never redefines)
- Adds dependency: SP-REVIEW-TOKEN-ESTIMATE -> SP-USAGE-LEDGER (the measured side of the comparison; the `review_runs` columns)
- Adds dependency: SP-REVIEW-TOKEN-ESTIMATE -> SP-REFUTER-JUDGES-WHAT-IT-CAN (`refuter_max_judged`, `refuter_context_tokens` give the refuter ceiling; `refuter_evidence` re-exports the estimator name)
- Adds dependency: SP-REVIEW-TOKEN-ESTIMATE -> SP-REVIEW-FILE-SELECTION (the refusal names the files; the estimate follows selection)
- Adds dependency: SP-REVIEW-TOKEN-ESTIMATE -> SP-UNIVERSAL-PROJECT-CONFIG, SP-DB-PATH-XDG, SP-CLI-STDOUT-ONE-JSON, SP-ORCH-DOCSTRING (the knob, the one JSON)
- `token_estimate.py` imports `providers.budget` and `project.config` only; `refuter_evidence.TOKEN_ESTIMATOR` is the re-export so the string has one definition.

## Diagram
N/A: one function before the fan-out, one comparison after it.

## Acceptance Criteria

- [ ] AC1: `pytest -q tests/unit/test_token_estimate.py` → green.

| Test | Pins | Mutation that turns it red |
|---|---|---|
| `test_estimator_is_the_budget_guards` | `estimate_review` uses `repoach.providers.budget.estimate_prompt_tokens` (a fake recording calls) and `estimator == ESTIMATOR_NAME` | a local `len // 3` |
| `test_estimate_precedes_the_first_call` | with a provider fake recording call order, `estimate` is logged before call 1 | the estimate is computed from usage rows |
| `test_cap_refuses_by_name_before_any_call` | cap 100 k, estimate 120 k → `EstimateRefused`, 0 provider calls, 0 forge writes | the check runs after the fan-out |
| `test_force_passes_the_cap_and_says_so` | `forced: true` in the JSON | `--force` is ignored |
| `test_refuter_ceiling_is_not_in_the_cap` | roles 90 k + refuter 320 k, cap 100 k → runs | `total_max` is compared |
| `test_estimate_flag_calls_nothing` | `--estimate` with a provider fake that raises | the dry run is taken |
| `test_usage_line_names_estimate_and_measured` | the stderr line carries both numbers and the estimator name | one is dropped |
| `test_run_row_keeps_both` | `review_runs.estimate_prompt_tokens` and `prompt_tokens` set | the column is not written |
| `test_estimator_name_has_one_definition` | `token_estimate.ESTIMATOR_NAME is budget.ESTIMATOR_NAME is refuter_evidence.TOKEN_ESTIMATOR`; `grep` of G5 → one file | a second literal |

- [ ] AC2: `pytest -q tests/unit/test_project_config.py -k max_prompt_tokens` → green: 0 and 10 000..5 000 000 parse, 9 999 is a `ConfigError`, the env var outranks the file. Mutation: drop the lower bound.
- [ ] AC3: `repoach arch check --base develop` → ok.

## Success conditions
- C1: on the planted corpus, every case prints an estimate before its first call and `measured prompt ≤ estimate max` holds for 10/10 (the estimate is a ceiling of the prompt sent; cached tokens count as prompt).
- C2: `review pr --estimate` on a recorded Yoob fixture exits 0 in under 5 s with zero provider calls.
- C3 (0.2): on the same ten cases, the per-role estimate equals the figure the budget guard admitted the call with, to the token; oracle: `anthropic_spend` rows against `estimate.roles`.

## Owner decisions needed
- O1: the default of `max_prompt_tokens` (0 proposed: a cap is the project's choice) and whether Yoob's `repoach.toml` sets one.
- O2: whether the estimate should also be posted in the archive comment (proposed: no; stderr and JSON suffice).

## Open Questions
- Q1: O1 and O2. The estimator's owner is decided (0.2): SP-ANTHROPIC-BUDGET-GUARD.
