---
id: SP-CC-SUBPROCESS-KILL-ON-CANCEL
title: Kill the claude_code CLI subprocess on every stream exit path, including cancellation
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code:
    - tests/unit/test_claude_code_client_kill_on_cancel.py
  resources: []

depends_on: [SP-CC-SYSPROMPT-FILE, SP-PROCESS-REGISTRY-WIRE, SP-PROVIDER-INIT-DEDUP]
provides_to: []

constraints: {}
---

# Kill the claude_code CLI subprocess on every stream exit path, including cancellation

## Intent

`ClaudeCodeProvider.stream_response` spawns a `claude -p` child process
and awaits its output, but nothing on the cancellation path ever kills
that child: an `asyncio.CancelledError` (or `GeneratorExit`) propagating
through the generator, and even the provider's own internal
`asyncio.TimeoutError`, both fall through to a `finally` block that only
deregisters the PID from the atexit safety net and cleans up the
sysprompt tempfile — the subprocess itself keeps running, unsupervised,
burning Max-plan quota and CPU. Make the `finally` block actually
terminate the child on every exit path, before it deregisters the PID.

## Context

Finding #8 (implementable findings sweep), re-verified live against
`origin/develop` on 2026-07-24 — all cited lines match exactly:

- `src/repoach/llm_proxy/providers/claude_code/client.py:159-174` spawns
  `proc = await asyncio.create_subprocess_exec(...)`, calls
  `register_pid(proc.pid)`, then awaits
  `asyncio.wait_for(proc.communicate(...), timeout=self._subprocess_timeout)`.
- `client.py:247-248`:
  `except (asyncio.CancelledError, GeneratorExit): raise` — re-raises
  without touching `proc` at all.
- `client.py:249-256` also catches the provider's OWN
  `asyncio.TimeoutError` (from the `wait_for` above) and raises
  `ProviderError` — again without touching `proc`; the subprocess that
  is still running past its own configured `subprocess_timeout` is
  never killed today.
- `client.py:272-284` — the only `finally` block on this code path:
  ```python
  finally:
      if proc is not None:
          unregister_pid(proc.pid)
      if sysprompt_path is not None:
          ...
  ```
  It never calls `proc.kill()` / `proc.terminate()` / `proc.wait()`.
- `src/repoach/llm_proxy/cli/process_registry.py:39-43` —
  `unregister_pid` only does `_pids.discard(int(pid))`; it removes the
  PID from the `kill_all_best_effort` atexit set. Combined with the
  above, a cancelled/timed-out stream removes the process from the
  ONLY safety net that could ever reap it, while leaving the process
  alive: this is strictly worse than not deregistering at all.
- `src/repoach/llm_proxy/api/services.py:519-521` gives `claude_code` a
  floor timeout (`effective_timeout = max(remaining, subprocess_timeout)`)
  instead of the shared dispatch budget, but does not make it immune to
  being cancelled from further upstream (a client disconnect, an outer
  `asyncio.wait_for` at a higher layer). `services.py:571-576` wraps
  every candidate's `stream_response` peek in
  `await asyncio.wait_for(peek_for_content(stream, ...), timeout=effective_timeout)`
  — a timeout here throws `CancelledError` into the `claude_code`
  generator exactly like any other provider, landing on the
  do-nothing `except (asyncio.CancelledError, GeneratorExit): raise`
  path above.
- SP-CC-SYSPROMPT-FILE and SP-PROCESS-REGISTRY-WIRE both already touch
  this same `finally` block (sysprompt cleanup, PID
  register/deregister respectively) but neither owns the file in
  frontmatter (`owns.code: []` on both); this spec is an in-place
  addition to the same block, tracked as a dependency rather than a
  claimed file per the existing convention on this module.
- Implementation reconciliation: the promised test file imports
  `ProviderConfig` from `providers/base.py`, which
  SP-PROVIDER-INIT-DEDUP owns; `SP-ARCH-EDGE-GATE` flagged the
  undeclared edge at commit time, so `SP-PROVIDER-INIT-DEDUP` was
  added to `depends_on` alongside the two above.

This is a live resource/cost leak today — independent of any future
hedging work — and it is the concrete blocker for ever safely
cancelling a losing `claude_code` hedge branch under LEVER-1.

## Goals

- G1: on `asyncio.CancelledError` / `GeneratorExit` reaching the
  `finally` block, the still-running `claude` child process is
  terminated (not just deregistered) before the exception continues
  propagating.
- G2: on the provider's own `asyncio.TimeoutError` (the existing
  `subprocess_timeout` cap), the child process is terminated the same
  way — this path has the identical pre-existing leak and gets the
  same fix.
- G3: termination is graceful-then-forceful: `proc.terminate()`
  (SIGTERM) first, a short bounded grace period, then `proc.kill()`
  (SIGKILL) if the child has not exited; `proc.wait()` is awaited so
  the child is reaped (no zombie).
- G4: the kill step runs unconditionally in `finally`, ahead of
  `unregister_pid`, so a PID is never removed from the atexit safety
  net while the process it names is still alive.
- G5: killing an already-exited process (`proc.returncode is not
  None`) or one that has already vanished (`ProcessLookupError`) is a
  silent no-op — no new exception surfaces from cleanup on the clean
  path.

## Non-Goals

- NG1: no behavior change beyond the kill-on-exit fix — the nominal
  (non-cancelled, non-timed-out) success path's return value, SSE
  event sequence, and logging are unchanged.
- NG2: no change to `process_registry.py`'s `register_pid` /
  `unregister_pid` / `kill_all_best_effort` semantics — this spec adds
  a kill call at the call site, it does not touch the registry module.
- NG3: no work on LEVER-1 hedging itself — this spec only removes the
  blocker (the orphan leak) so a future hedge-cancellation spec can
  build on a `finally` block that is already exit-safe.
- NG4: no change to `services.py`'s timeout/budget computation
  (`effective_timeout`, `dispatch_total_budget_s`) — the cancellation
  this spec defends against is triggered from there, but the trigger
  logic itself is out of scope.
- NG5: no retry-on-kill-failure logic; the kill is best-effort logged,
  never re-raised as a new user-facing error.

## Interface

`src/repoach/llm_proxy/providers/claude_code/client.py`:

```python
_SUBPROCESS_KILL_GRACE_S: float = 2.0


async def _kill_subprocess_on_exit(
    proc: asyncio.subprocess.Process, req_tag: str
) -> None:
    """Ensure ``proc`` is not left running past the end of a stream call.

    Best-effort, never raises: sends SIGTERM and waits up to
    ``_SUBPROCESS_KILL_GRACE_S`` seconds for a clean exit; escalates to
    SIGKILL and waits again if the child is still alive. A no-op if the
    process has already exited or already vanished.

    Args:
        proc: The spawned ``claude`` CLI child process.
        req_tag: Request-id log suffix for correlating cleanup log
            lines with the originating request.
    """
```

Called from the existing `finally` block at `client.py:272-284`,
before `unregister_pid(proc.pid)`.

## Behavior

### Nominal

- `stream_response` completes normally (`proc.returncode == 0` already
  observed via `communicate()` returning) → `_kill_subprocess_on_exit`
  sees `proc.returncode is not None` and returns immediately; no signal
  sent; behavior identical to today.

### Edge cases

- The child exits on its own between the `finally` block starting and
  the kill call running (a benign race) → `proc.terminate()` /
  `proc.kill()` raise `ProcessLookupError`, caught and swallowed.
- SIGTERM does not stop the child within the grace period (hung CLI) →
  escalate to SIGKILL, then `await proc.wait()` unconditionally so the
  child is reaped.

### Failure scenarios

- `asyncio.CancelledError` / `GeneratorExit` raised while
  `proc.communicate()` is in flight (upstream cancellation, e.g. the
  `services.py:571-576` `wait_for(effective_timeout)` firing) → the
  `except (asyncio.CancelledError, GeneratorExit): raise` re-raises as
  today, but the `finally` block now kills the child before
  `unregister_pid` runs and before the exception continues
  propagating to the caller.
- The provider's own `asyncio.TimeoutError` (`subprocess_timeout`
  exceeded) → same kill-then-deregister sequence; previously this path
  raised `ProviderError` while leaving the child running past its own
  configured cap.

## Acceptance Criteria

- [ ] AC1: unit — cancelling the task driving
  `ClaudeCodeProvider.stream_response` while a real (short-lived
  fake-CLI) child process is mid-`communicate()` results in the OS
  process being dead (`os.kill(pid, 0)` raises `OSError`) within the
  grace period plus a small buffer, not merely absent from
  `process_registry._pids`.
- [ ] AC2: unit — a `stream_response` call whose fake CLI sleeps longer
  than a small injected `subprocess_timeout` (the provider's own
  internal timeout, no external cancellation involved) also results in
  the OS process being dead after the call raises `ProviderError`.
- [ ] AC3: unit — a `stream_response` call that completes normally
  (fake CLI exits immediately with a valid payload) sends no signal —
  assert `proc.terminate`/`proc.kill` are never invoked on the happy
  path (patch and assert not-called on the real subprocess wrapper, or
  equivalently assert the fake CLI's own natural exit code is what
  `communicate()` reports, not a killed-process code).
- [ ] AC4: promised tests —
  `tests/unit/test_claude_code_client_kill_on_cancel.py::test_stream_task_cancellation_kills_subprocess`,
  `::test_internal_timeout_kills_subprocess`,
  `::test_clean_completion_never_signals_subprocess`. Each must FAIL
  on pre-change code (today: the cancelled/timed-out fake CLI process
  is still alive and `os.kill(pid, 0)` succeeds after the call
  returns/raises).
- [ ] AC5: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  existing `tests/unit/test_process_registry_wire.py` and
  `tests/unit/test_claude_code_client_argv.py` remain green
  unmodified.

## Architecture Impact

- Adds/Removes dependency: none — in-place addition to the existing
  `finally` block of `ClaudeCodeProvider.stream_response`
  (`client.py`, currently unowned by any spec's `owns.code`; tracked
  via `depends_on` on SP-CC-SYSPROMPT-FILE and SP-PROCESS-REGISTRY-WIRE
  since both already touch the same block). One new module-level
  helper function and one new test file; no new cross-module import,
  no new package.
- New / changed coupling, cycles, or shared state: none — the helper
  operates only on the `proc` handle already local to
  `stream_response`; it does not read or write `process_registry`
  module state (that ordering — kill before `unregister_pid` — is a
  call-site sequencing change, not a new coupling).

## Diagram

N/A (in-place fix, single function + one call-site change).
