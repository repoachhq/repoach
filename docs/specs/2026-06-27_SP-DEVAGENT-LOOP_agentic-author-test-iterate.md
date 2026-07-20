---
id: SP-DEVAGENT-LOOP
title: Developer agentic author→test→iterate loop (DEVAGENT slice 2)
version: 0.1
status: draft
author: agent
created: 2026-06-27
updated: 2026-06-27

owns:
  code:
    - src/repoach/review/devagent_loop.py
    - src/repoach/review/secret_env.py
  resources:
    - prompts/review/developer_agentic_0.1.0.md

depends_on:
  - SP-DEVAGENT-TOOLS

provides_to: []
constraints: {}
---

# SP-DEVAGENT-LOOP — the spine: a Developer that authors, tests, and iterates

## Intent
Slice 2 of the real-coding-agent arc (umbrella `docs/devagent_architecture.md`).
Turn the Developer from a one-shot JSON generator into a genuine multi-turn
tool-using coding agent. Today `Developer.respond` calls
`AgentLoop.run_oneshot` (zero tools, one turn → one JSON fix-plan) and all the
real work — applying the fixes, running the gates, reverting, retrying — is bolted
on *externally* in `dev_runner.execute_plan_step`. This slice moves that work
*inside* the model's loop: the Developer reads the tree, writes/edits files, runs
the tests and ruff, reads the results, and fixes forward until green — exactly the
proven pattern the Planner already uses with read-only tools
(`Planner._plan_via_proxy` → `AgentLoop.run(tools=…)`).

Per the operator's calibration (2026-06-27), this slice **wires the live path**:
`execute_plan_step` is rewired to drive the agentic loop, not just to host an
unused method. That absorbs one piece of the later WIRE slice — the destructive
`revert_working_tree` (reset --hard + `git clean` of untracked code) is **replaced
on this path by a non-destructive fix-forward retry**, because in an agentic loop
the partial work *is* the progress; wiping it on every red attempt throws away the
model's state and is the known untracked-wipe footgun.

## Context
Reused as-is (no change): the brain (`AgentLoop` → proxy → chains + claude_code,
CODER tier), the slice-1 author/verify tools (`devagent_tools.make_developer_tools`
— `write_file`/`edit_file`/`run_tests`/`run_ruff`, jailed + never-raise), the
read-only exploration tools (`planner_tools.make_planner_tools` —
`list_dir`/`read_file`/`grep_repo`), the per-step brief (`build_step_brief`), and
the authoritative gates (`check_python_syntax`, `check_imports`, `run_ruff_gate`,
`run_repo_lint_gates`, `run_promised_tests`, `heal_inline_comments`, `commit_all`)
in `coder_loop`/`dev_runner`.

The model self-runs `run_tests`/`run_ruff` in the loop, but that self-report is not
authoritative (it may stop early, hit the turn budget, or test a narrow target
while CI runs the full 3.11/3.13 suite). So `dev_runner` re-verifies once after the
loop with the existing gates and commits only on green. This authoritative gate is
*only* the mechanical re-run of existing gates; the AC-selector and LLM-judge
semantic verification are deferred to SP-DEVAGENT-SELFVERIFY (slice 3).

## Goals
- G1: A new owned module `review/devagent_loop.py` exposing `DevLoopResult` and
  `run_agentic_step(loop, *, system, brief, repo_root, allowed_paths, repo_tree)`
  — it assembles the toolbox (planner read tools ∪ developer write/verify tools
  jailed to `allowed_paths`), runs `AgentLoop.run(tools=…)`, and returns a
  `DevLoopResult` (final text, turns, tokens, model, tool_calls_made).
- G2: `Developer.develop_step(...)` — the agentic public entrypoint mirroring
  `respond`: renders the new agentic persona as the system prompt and delegates to
  `run_agentic_step` over the Developer's own `AgentLoop`.
- G3: `make_developer_tools(repo_root, allowed_paths=None)` — when `allowed_paths`
  is given, `write_file`/`edit_file` additionally refuse any path outside that set
  with an error string (no write), enforcing the per-step file contract *in-loop*
  so the model self-corrects. `None` preserves slice-1 behaviour.
- G4: `dev_runner.execute_plan_step` rewired to: build the toolbox jailed to
  `step.files`, drive `develop_step`, then post-loop — discover changed files via
  git, reject out-of-contract changes, heal inline comments, run the authoritative
  gate chain, and on green `commit_all(step.commit_message)`. On a red gate, retry
  **fix-forward** (re-enter the loop with the gate feedback appended to the brief,
  partial work preserved) up to `_MAX_STEP_ATTEMPTS`; never `revert_working_tree`
  on this path.
- G5: A new agentic persona `prompts/review/developer_agentic_0.1.0.md` instructing
  tool use (read → write/edit → run_tests/run_ruff → iterate to green), the file
  contract, the golden rule, and "stop only when tests and ruff pass".
- G6 (adversarial-review finding S4): every subprocess that *executes*
  agent-authored code must run with a secret-scrubbed environment. The in-loop
  scrub (slice 1, `run_tests`) is otherwise illusory — the runner's authoritative
  reruns (`dev_runner.run_pytest_selectors`, `coder_loop.run_pytest` via the
  matrix) re-execute the same code. A shared leaf `review/secret_env.py`
  (`scrubbed_env`, imports only `os` → no cycle) is used at all three sites.
  `import_gate.check_imports` is AST-only and runs no agent code, so it is exempt.

## Non-Goals
- NG1: No semantic self-verification (AC-selector presence, LLM judge) — that is
  SP-DEVAGENT-SELFVERIFY (slice 3).
- NG2: No spec decomposition — that is SP-DEVAGENT-DECOMPOSE (slice 4).
- NG3: No CLI / review-handoff changes and no removal of the *other* callers of
  `revert_working_tree` — that is SP-DEVAGENT-WIRE (slice 5). This slice only
  stops `execute_plan_step` from reverting destructively on its own path.
- NG4: The legacy `Developer.respond` one-shot path stays in place (other callers
  may use it); it is not deleted here.

## Interface
- `review.devagent_loop.DevLoopResult` — dataclass:
  `text, turns, tokens_used, elapsed_s, model_used, tool_calls_made`.
- `review.devagent_loop.run_agentic_step(loop: AgentLoop, *, system: str,
  brief: str, repo_root: Path, allowed_paths: Iterable[str],
  repo_tree: str = "") -> DevLoopResult`.
- `review.reviewer.Developer.develop_step(*, brief: str, repo_root: Path,
  allowed_paths: Iterable[str], repo_tree: str = "", spec_id: str | None = None)
  -> DevLoopResult`.
- `review.devagent_tools.make_developer_tools(repo_root: Path | None = None,
  allowed_paths: Iterable[str] | None = None) -> list[ToolDef]` (additive param).

## Behavior
- Given a step whose `files` contract is `{A, B}`, the agent may read anywhere
  (read tools) but writing to `C ∉ {A, B}` returns an error string and no write;
  the model retries within the contract.
- The loop ends when the model stops calling tools (or hits the turn budget).
- Post-loop, `execute_plan_step` runs the authoritative gates; on green it commits
  the step with `step.commit_message`; on red it re-enters the loop with the gate
  tail as feedback, preserving the partial tree, up to the attempt cap.
- On a step that exhausts its attempts, the step fails (`StepOutcome.ok=False`)
  with the last gate tail in `reason`; the working tree is **left as-is** (no
  destructive clean), and `run_developer_session` stops at that step without
  pushing — the partial work survives for inspection.
- A step that promises unit tests which do not exist after the loop fails the same
  way (no destructive revert).

## Architecture Impact
- Owns one new leaf module (`devagent_loop.py`) and one new resource (the agentic
  persona). Import edges: `devagent_loop` → `agent_engine.agent_loop`,
  `review.devagent_tools`, `review.planner_tools`. `depends_on: [SP-DEVAGENT-TOOLS]`
  (it composes the slice-1 toolbox).
- Edits (wiring, not ownership): `reviewer.py` gains `Developer.develop_step`;
  `devagent_tools.py` gains the `allowed_paths` param; `dev_runner.py`
  `execute_plan_step` is rewired to the agentic driver.

## Acceptance Criteria
- [ ] AC1: `tests/unit/test_devagent_loop.py` covers: `run_agentic_step` assembles
  read + write/verify tools and passes them to `AgentLoop.run` (FakeLoop captures
  `tools=`); `DevLoopResult` carries the loop's text/turns/tokens/model;
  `allowed_paths` enforcement in `make_developer_tools` (in-contract write ok,
  out-of-contract write refused with no write, `None` unchanged).
- [ ] AC2: `execute_plan_step` tests with a fake Developer whose `develop_step`
  performs tool-style writes: a green step commits with `step.commit_message`; an
  out-of-contract write is rejected; a red gate triggers a fix-forward retry (no
  `revert_working_tree` call) and an exhausted step leaves the tree uncommitted and
  unwiped.
- [ ] AC3: ruff + format + no-inline-comments + `arch check` (edge-honesty) + full
  `pytest tests/unit` green under 3.11 and 3.13.

## Open Questions
- The authoritative post-loop gate duplicates work the model already did in-loop;
  if this proves wasteful, slice 3 (SELFVERIFY) may subsume the mechanical re-run.
