---
id: SP-CC-SYSPROMPT-FILE
title: claude_code system prompt travels via file, never argv
version: 0.1
status: approved
author: jfaye (proxy log forensics 2026-07-18, 9 recorded E2BIG failures)
created: 2026-07-18
updated: 2026-07-18

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# claude_code system prompt travels via file, never argv

## Intent

The `claude_code` provider passes the system prompt as a single argv
argument (`cmd += ["--system-prompt", system_prompt]`,
`src/ferova/llm_proxy/providers/claude_code/client.py:130`). Linux
caps one argv element at `MAX_ARG_STRLEN` = 131072 bytes, so any
system prompt above ~128 KiB — reviewer benches and self-verify
judge dossiers routinely are — kills the spawn with
`OSError: [Errno 7] Argument list too long` before the CLI even
starts. `logs/llm_proxy.log` records 9 such failures across prior
proxy runs; each one silently amputates the chain's last-resort hop
and degrades Developer self-verify to mechanical-only. The main
prompt already travels via stdin precisely to avoid this class
(pinned by `tests/unit/test_claude_code_tools_emulation.py::
test_prompt_travels_via_stdin_never_argv`); the system prompt must
get the same treatment via the CLI's `--system-prompt-file` flag
(present and file-validated in the deployed CLI 2.1.214).

## Context

`ClaudeCodeProvider.stream_response` builds `cmd` (`client.py:120`),
appends `--system-prompt <text>` when a system prompt is present
(`client.py:129-130`), logs `shlex.join(cmd)` — the full system
prompt included — in the `CLAUDE_CODE_STREAM` line
(`client.py:133-139`), then spawns via
`asyncio.create_subprocess_exec` (`client.py:146`) with the main
prompt piped through stdin. The provider already owns a private
scratch directory (`self._workdir`, a `ferova_claude_code_` tempdir
created at init, `client.py:86`) used as the subprocess cwd — a
natural home for a per-request system-prompt file. Concurrency > 1
is possible (`GlobalRateLimiter` slots), so the file name must be
unique per request. Related init-time observability gap, same
failure class as the 2026-07-18 boot-PATH outage: `shutil.which`
falling back to the bare name (`client.py:82`) is silent — the
first hint of a missing binary is a per-call 500 much later.

## Goals

- G1: when a system prompt is present, the provider writes it to a
  unique file under `self._workdir` and passes
  `--system-prompt-file <path>`; the system prompt string never
  appears in argv. The file is removed after the subprocess
  completes, on every path (success, non-zero exit, timeout, spawn
  failure).
- G2: the `CLAUDE_CODE_STREAM` log line stops serialising
  prompt-bearing text: it reports `system_prompt_chars` alongside
  `prompt_chars`, and the joined command it logs contains flags and
  paths only (log hygiene — today the full system prompt lands in
  the log file).
- G3: init logs one loud warning when `shutil.which(cli_path)`
  returns `None` (binary not on the service's PATH), naming the
  requested `cli_path` — so the next boot-PATH regression is
  diagnosable from the log head instead of per-call 500 forensics.

## Non-Goals

- NG1: no change to the main-prompt stdin transport.
- NG2: no size threshold — the file path is used for every
  non-empty system prompt (one branch, no cliff).
- NG3: no fix for the deployment PATH itself (systemd user-unit
  boot ordering) — that is operator-side ops config.
- NG4: no subprocess lifecycle changes beyond the file cleanup (the
  timeout/kill semantics stay as they are).

## Assumptions

- A1: the deployed `claude` CLI supports `--system-prompt-file`
  (verified live on 2.1.214: unknown paths yield "System prompt
  file not found", proving the flag parses).
- A2: `self._workdir` outlives every in-flight request (created at
  init, never cleaned mid-run — `cleanup()` is a no-op).

## Interface

N/A (in-place fix — `stream_response` signature, SSE shape, and the
provider registry wiring are unchanged).

## Behavior

### Nominal

Request with system prompt → file written under `self._workdir`
with a per-request unique name → `--system-prompt-file <path>` in
argv → subprocess runs → file removed. Request without system
prompt → no flag, no file (unchanged).

### Edge cases

- System prompt larger than 128 KiB → works (the whole point);
  argv stays small and constant-size.
- Concurrent requests → distinct file names, no cross-request
  clobbering.
- Empty-string system prompt → treated as absent (current
  behaviour preserved).

### Failure scenarios

- File write fails (workdir vanished, disk full) → surfaced through
  the existing `OSError` → `ProviderError` path ("claude CLI spawn
  failed" family), SSE error emitted, no crash, no orphan file.
- Subprocess timeout → the existing timeout path fires AND the
  system-prompt file is still removed.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of one
  provider module; stdlib only (`tempfile`/`uuid`/`os` are already
  the module's register).
- New / changed coupling, cycles, or shared state: none — the file
  lives in the provider's already-private workdir.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — new file
  `tests/unit/test_claude_code_sysprompt_file.py`, following the
  fake-exec capture pattern of
  `tests/unit/test_claude_code_tools_emulation.py`:
  `test_system_prompt_travels_via_file_never_argv` (argv contains
  `--system-prompt-file` and a path; the system prompt text appears
  in NO argv element; the file's content equals the system prompt
  at spawn time),
  `test_sysprompt_file_removed_after_completion` (file gone after
  the stream ends),
  `test_no_sysprompt_flag_when_absent` (no `--system-prompt*` argv
  and no file when the request carries no system prompt),
  `test_which_failure_logs_loud_warning` (constructing the provider
  with an unresolvable `cli_path` emits the G3 warning).
- [ ] AC2 (INTEGRATION): new file
  `tests/integration/test_claude_code_sysprompt_e2big.py::
  test_oversized_system_prompt_survives_real_spawn` — a REAL
  subprocess spawn (no fake exec) of a minimal executable script
  standing in for the CLI at the process boundary (writes its argv
  to a capture file, prints a valid `--output-format json` payload),
  driven with a 200 000-character system prompt: the stream
  completes, the captured argv carries `--system-prompt-file`, and
  the system prompt text is absent from argv. This test fails with
  `[Errno 7]` on the pre-fix code — it is the regression pin for
  the whole class.
- [ ] AC3: the `CLAUDE_CODE_STREAM` log line reports
  `system_prompt_chars` and no longer embeds system-prompt text
  (G2), asserted in AC1's argv/log capture or a dedicated check in
  the same unit file.
- [ ] AC4: `ruff` + `ruff format --check` green; zero inline
  comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `pytest tests/unit` green — including the existing
  `test_claude_code_tools_emulation.py` suite unchanged.

## Open Questions

None — the ops-side companion (boot PATH of the systemd user unit /
`FEROVA_CLAUDE_CODE_CLI_PATH` in `.env`) is tracked operator-side,
outside this spec (NG3).
