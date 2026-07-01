# SP-PLANNER-CC-EXPLORE — Planner delegated-exploration mode (builder slice)

## Metadata

- **Status**: OPEN
- **Priority**: P2
- **Owner**: operator
- **Executor**: hand-implemented (adds a `prompts/review/` persona —
  bot-forbidden path — and subprocess/security-sensitive code)
- **Opened**: 2026-06-08

## Why

Option 3 of the builder chantier (design session 2026-06-07). The
proxy Planner explores via the AgentLoop ↔ local-tools loop over the
NIM/coder chain — capable, but its reasoning tier is bounded by what
the proxy chains can do with tools (the opus chain can't tool-call at
all). This slice adds an EXPLICIT alternative: run one `claude -p`
session in the repository with the CLI's NATIVE read-only tools
(Read/Glob/Grep/LS) on the operator's Max subscription, so the
Planner can reason at full Claude quality with first-class
exploration and the CLI's own session cache.

Probed live 2026-06-08: `claude -p "<task>" --allowedTools
"Read,Glob,Grep,LS" --add-dir <repo> --permission-mode default
--output-format json --model sonnet` ran in the repo, used its tools
(3 turns), and returned clean JSON in the envelope's `result` field
in 10s.

This is an **explicit mode, never a transparent chain link**: the
execution model (the CLI drives its own exploration) and the audit
trail (tool calls happen inside the CLI, not the AgentLoop) differ
from the proxy path, so it is opt-in per invocation.

## What

### `src/ferova/review/planner_cc.py` (new)

- `CcExploreResult` dataclass: `text` / `num_turns` / `duration_ms`
  / `is_error` / `error`.
- `run_cc_exploration(*, prompt, repo_root, model, allow_tools=True,
  cli_path=None, timeout_s=600) -> CcExploreResult`: spawns
  `claude -p` with `cwd=repo_root`. When `allow_tools`, passes
  `--allowedTools "Read,Glob,Grep,LS"` + `--add-dir repo_root`
  (READ-ONLY: never Write/Edit/Bash). Always `--output-format json
  --permission-mode default --model <model>`. Parses the envelope,
  returns `result` as `text`. Every failure (timeout, non-zero exit,
  non-JSON envelope, `is_error: true`) returns `is_error=True` with a
  message — never raises.

### `src/ferova/review/planner.py`

- `Planner.__init__` gains `explore_via: Literal["proxy",
  "claude_cli"] = "proxy"` and `cc_model: str = "sonnet"`.
- `plan()` branches: `"proxy"` keeps the current AgentLoop path;
  `"claude_cli"` reads the CC persona, substitutes `{SPEC_PLAN}`,
  and runs the same 3-attempt parse/validate/refine loop where
  exploration AND refinement are `run_cc_exploration` calls
  (refinement passes `allow_tools=False` — no re-exploration). The
  audit dict carries `explore_via`, `model_used="claude-cli/<model>"`,
  accumulated turns/elapsed.
- The shared `_parse_and_validate` / `_refine_prompt` helpers are
  reused unchanged, so the strict plan-form contract (forward-ref,
  promised-tests-created, etc.) applies identically in both modes.

### `prompts/review/planner_cc_0.1.0.md` (new, hand-shipped)

Same plan-quality bar / output contract / test-coupling rule as
`planner_0.2.0.md`, but the tool section describes the CLI's native
Read/Glob/Grep/LS tools (the CLI explores the repo directly), with a
`{SPEC_PLAN}` placeholder and no `{REPO_TREE}`.

### CLI

`ferova plan <id> --explore-via {proxy,claude_cli}` (default
`proxy`) and the same option on `review plan`.

## Files in scope

- `src/ferova/review/planner_cc.py` (new)
- `src/ferova/review/planner.py`
- `prompts/review/planner_cc_0.1.0.md` (new)
- `src/ferova/cli/review_cmds.py`
- `tests/unit/test_review_planner_cc.py` (new)
- `tests/unit/test_review_planner.py`

## Out of scope

- Wiring delegated exploration into `ferova develop`'s
  `load_or_produce_plan` (a later opt-in; this slice ships the mode +
  the `plan` CLI).
- Any change to the proxy Planner path's behaviour.
- Write/Edit/Bash tools for the CLI — read-only only, always.

## Smoke scenario

### Setup

`claude` CLI authenticated (Max subscription); run from the repo.

### Execute

```bash
ferova plan SP-INTEGRATION-STAGE --explore-via claude_cli
```

### Expected

Exit 0; `docs/plans/SP-INTEGRATION-STAGE.md` written and parseable;
the printed JSON reports `written: true` and a `model_used` of the
form `claude-cli/sonnet`.

## Definition of Done

- `run_cc_exploration` behaves as specified (read-only tools, JSON
  envelope parsed, never raises) — unit-tested with a faked
  subprocess (success, is_error, timeout, non-JSON, non-zero exit).
- `Planner(explore_via="claude_cli")` produces a validated plan via a
  patched `run_cc_exploration`, runs the same parse/retry loop, and
  refinement passes `allow_tools=False`.
- CC persona exists with the `{SPEC_PLAN}` placeholder and the
  ActionPlan contract vocabulary; describes the native tools.
- `ferova plan <id> --explore-via claude_cli` wired.
- `ruff` + full `pytest tests/unit` green; zero inline comments.

## Commit plan

1. `feat(review): claude -p delegated exploration backend (read-only)`
2. `feat(review): Planner explore_via=claude_cli mode + CLI flag`
3. `feat(prompts): planner_cc persona 0.1.0`
4. `test(review): planner_cc subprocess + CC-mode planner suites`

## Risks

- Granting the CLI repo read access: mitigated by the hard read-only
  tool allowlist (no Write/Edit/Bash) and `--add-dir` scoped to the
  repo root. Sentinel should confirm no mutating tool is reachable.
- Max-quota cost: one exploration + up to two refinements per plan;
  acceptable for an opt-in mode, and the refinement passes no tools.
