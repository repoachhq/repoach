---
id: SP-PROCESS-REGISTRY-WIRE
title: Register spawned CLI subprocesses so the cleanup safety net actually reaps them
version: 0.1
status: draft
author: jfaye (Ferova audit 2026-07-13)
created: 2026-07-13
updated: 2026-07-13

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Register spawned CLI subprocesses so the cleanup safety net actually reaps them

## Intent

The subprocess-cleanup safety net is a guaranteed no-op: nothing ever calls
`register_pid`, so the registry is always empty and `kill_all_best_effort` reaps
nothing. A Ctrl+C during a `claude -p` call (timeout up to 600s) orphans the
subprocess and keeps burning Max quota. Register the PID at spawn time and
deregister on clean exit.

## Context

`src/ferova/llm_proxy/cli/process_registry.py:31-44` defines `register_pid` /
`unregister_pid`; `kill_all_best_effort` (`process_registry.py:46-77`) kills
whatever is in `_pids`. The reaper IS wired into shutdown: `__main__.py:30,42`
calls it in a `finally`, and `ensure_atexit_registered` registers it with
`atexit`. But grep confirms NOTHING calls `register_pid` anywhere in `src/`
(only its own definition and the `unregister_pid` sibling), so `_pids` is always
empty and `kill_all_best_effort` returns immediately at the `if not pids`
guard (`process_registry.py:57-58`).

The `claude` CLI subprocess is spawned at
`src/ferova/llm_proxy/providers/claude_code/client.py:146` via
`asyncio.create_subprocess_exec(...)` (inside a concurrency slot,
`client.py:143`), with `self._subprocess_timeout` up to 600s
(`client.py:153-156`). Its `proc.pid` is never registered. Audit 2026-07-13
finding M20.

## Goals

- G1: the spawned subprocess PID is registered via `register_pid(proc.pid)`
  immediately after `create_subprocess_exec` returns (`client.py:146`).
- G2: the PID is deregistered via `unregister_pid(proc.pid)` on every clean exit
  path — normal completion, timeout, and cancellation — using a `finally` so the
  registry never accumulates dead PIDs.
- G3: after wiring, `kill_all_best_effort` on an interrupt actually targets the
  live `claude` subprocess (the registry is non-empty during the call window).

## Non-Goals

- NG1: no change to `process_registry`'s kill mechanics (`taskkill` / `os.kill`)
  or its atexit/finally wiring in `__main__.py`.
- NG2: no change to the subprocess timeout or the concurrency slot.
- NG3: no cross-platform PID-group handling beyond what the registry already
  does.

## Assumptions

- A1: `create_subprocess_exec` returns a `proc` with a valid `.pid` before the
  awaited `communicate`, so registration can happen at spawn and deregistration
  in `finally`.
- A2: `register_pid`/`unregister_pid` are threadsafe (guarded by `_lock`,
  `process_registry.py:35,42`) and safe to call from the async transport.

## Interface

N/A (in-place fix, no signature change). `claude_code/client.py` imports
`register_pid`/`unregister_pid` from `..cli.process_registry` (leaf module,
imports only stdlib + loguru — no cycle) and calls them around the spawn.

## Behavior

### Nominal

Spawn -> `register_pid(proc.pid)` -> `communicate` -> in `finally`,
`unregister_pid(proc.pid)`. On clean completion the registry is emptied of this
PID before the transport returns.

### Edge cases

- Timeout (`asyncio.wait_for` raises `TimeoutError`) -> the existing timeout
  handling runs; the `finally` still deregisters after the process is killed by
  the timeout path.
- Cancellation (`CancelledError` from Ctrl+C mid-call) -> the PID is still in
  the registry when `kill_all_best_effort` runs from the atexit/finally net, so
  the orphan is reaped; the transport's own `finally` deregisters if it gets to
  run.

### Failure scenarios

- If `register_pid` is somehow skipped, behavior is no worse than today (the
  reaper was already a no-op). Fail CLOSED: registration happens on the same
  line region as the spawn so there is no window where a live PID is unknown to
  the registry during a long call.

## Architecture Impact

- Adds dependency: none new at the spec-graph level — in-place modification of
  `claude_code/client.py` (owned by an existing spec). New intra-package import
  `providers.claude_code.client` -> `cli.process_registry`; the registry is a
  stdlib-only leaf, so no cycle and no cross-owner edge of concern.
- New / changed coupling, cycles, or shared state: the client now writes to the
  process-registry shared set — the intended coupling; no cycle.

## Diagram

N/A (in-place fix).

## Acceptance Criteria

- [ ] AC1: unit — after the transport spawns a boundary-fake subprocess (a real
  short-lived child, e.g. `sleep`/`python -c "..."`, spawned via the real
  `create_subprocess_exec` path — NOT a monkeypatched Ferova function), its
  `pid` is present in the registry `_pids` during the call and absent after
  clean completion.
- [ ] AC2 (INTEGRATION): drive an interrupt scenario — register a real
  boundary-fake subprocess through the client's spawn path, then call
  `kill_all_best_effort()` and assert it targets that PID (the process is
  signalled/reaped and the registry is cleared). Exercises the real
  register -> reap chain end to end; no Ferova code is stubbed.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_process_registry_wire.py::test_spawn_registers_pid`,
  `::test_clean_exit_deregisters_pid`,
  `::test_kill_all_best_effort_reaps_registered_subprocess`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
