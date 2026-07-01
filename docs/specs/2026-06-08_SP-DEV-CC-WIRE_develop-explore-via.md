# SP-DEV-CC-WIRE — wire delegated exploration into `ferova develop`

## Metadata

- **Status**: OPEN
- **Priority**: P2
- **Owner**: operator
- **Executor**: hand-implemented (small cross-layer plumbing)
- **Opened**: 2026-06-08

## Why

SP-PLANNER-CC-EXPLORE (PR #329) shipped the Planner's
`--explore-via claude_cli` mode and wired it into `ferova plan`,
but explicitly left `ferova develop`'s plan-production path on the
proxy backend. This slice threads the exploration backend through the
develop pipeline so a full autonomous build can plan via the native
`claude -p` session on the Max quota.

## What

Thread `explore_via` / `cc_model` through the three layers that sit
between the `develop` CLI and `run_planner_session`:

1. `dev_runner.load_or_produce_plan` — add
   `explore_via: Literal["proxy", "claude_cli"] = "proxy"` and
   `cc_model: str = "sonnet"`; forward both to `run_planner_session`
   (only the produce path uses them; a committed plan still wins).
2. `dev_runner.run_developer_session` — add the same two params,
   forward them to `load_or_produce_plan`.
3. CLI `review_develop` (`ferova develop`) — add
   `--explore-via {proxy,claude_cli}` (default `proxy`) and
   `--cc-model` options, validate the backend (exit 2 on a bad
   value), forward to `run_developer_session`.

No behaviour changes on the default (`proxy`) path. The CC path
reuses the existing, tested `run_planner_session(explore_via=...)`.

## Files in scope

- `src/ferova/review/dev_runner.py`
- `src/ferova/cli/review_cmds.py`
- `tests/unit/test_review_plan_executor.py`
- `tests/unit/test_review_dev_cli_explore_via.py` (new)

## Out of scope

- Any change to the Planner, the CC backend, or the persona.
- Changing the default backend (stays `proxy`).

## Smoke scenario

### Setup

`claude` CLI authenticated; a spec with no committed plan.

### Execute

```bash
ferova develop SP-EXAMPLE --explore-via claude_cli --no-push
```

### Expected

The produced `docs/plans/SP-EXAMPLE.md` exists and its planning ran
via the CLI backend (the session's planner used `claude-cli/<model>`);
the develop pipeline then executes the plan as usual.

## Definition of Done

- `load_or_produce_plan` and `run_developer_session` accept and
  forward `explore_via` / `cc_model`; a committed plan still bypasses
  planning entirely.
- `ferova develop --explore-via claude_cli` reaches
  `run_planner_session(explore_via="claude_cli")` (verified by a test
  that patches `run_planner_session` and asserts the forwarded
  kwargs), and a bad `--explore-via` exits 2.
- `ruff` + full `pytest tests/unit` green; zero inline comments.

## Commit plan

1. `feat(review): thread explore_via/cc_model through load_or_produce_plan + run_developer_session`
2. `feat(cli): ferova develop --explore-via / --cc-model`
3. `test(review): develop CC-backend forwarding + bad-backend exit`

## Risks

- A committed plan must still short-circuit planning regardless of
  `explore_via` — the load path runs before any planner call, so the
  flags are inert when a plan already exists; a test pins that.
