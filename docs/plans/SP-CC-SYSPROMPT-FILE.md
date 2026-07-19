# SP-CC-SYSPROMPT-FILE — claude_code system prompt travels via file, never argv

Replace the argv-borne --system-prompt in ClaudeCodeProvider.stream_response with a per-request file under self._workdir passed via --system-prompt-file, clean it up on every exit path, scrub the system prompt from the CLAUDE_CODE_STREAM log line, and emit a loud init-time warning when shutil.which cannot resolve cli_path. Pins the fix with a new unit suite (argv/file/log/which) and a real-spawn integration test that drives a 200 KiB system prompt through the provider.

## Step 1 — G3: loud init warning when cli_path is unresolvable + first unit test

- **Files**: `src/ferova/llm_proxy/providers/claude_code/client.py`, `tests/unit/test_claude_code_sysprompt_file.py`
- **Action**: In ClaudeCodeProvider.__init__, after the existing `resolved_cli = shutil.which(cli_path) or cli_path` line, detect the unresolvable case (`shutil.which(cli_path) is None`) and emit a single `logger.warning("CLAUDE_CODE_CLI_UNRESOLVABLE: cli_path={!r} not found on PATH; subprocess spawns will fail with OSError", cli_path)` BEFORE assigning self._cli_path. Keep the existing fallback assignment so behaviour is unchanged. Create tests/unit/test_claude_code_sysprompt_file.py with a module-level helper that builds a provider with a given cli_path and captures loguru output via a temporary sink (`logger.add` appending records to a list, removed after the test — pytest's caplog does not capture loguru); add test_which_failure_logs_loud_warning that constructs the provider with cli_path="definitely-not-on-path-xyz" and asserts the warning text contains the requested cli_path and the substring CLAUDE_CODE_CLI_UNRESOLVABLE. Use the same fake-exec capture pattern as tests/unit/test_claude_code_tools_emulation.py (patch asyncio.create_subprocess_exec) so no real binary is needed.
- **Commit**: `feat(claude_code): warn loudly when cli_path is unresolvable at init`
- **Done when**: pytest tests/unit/test_claude_code_sysprompt_file.py::test_which_failure_logs_loud_warning passes
- **Unit tests**: `tests/unit/test_claude_code_sysprompt_file.py::test_which_failure_logs_loud_warning`

## Step 2 — G1+G2: system prompt travels via --system-prompt-file, log line scrubs text

- **Files**: `src/ferova/llm_proxy/providers/claude_code/client.py`, `tests/unit/test_claude_code_sysprompt_file.py`
- **Action**: In ClaudeCodeProvider.stream_response, replace the `if system_prompt: cmd += ["--system-prompt", system_prompt]` block with: when system_prompt is truthy, generate a unique filename via `self._workdir / f"sysprompt_{uuid.uuid4().hex}.txt"`, write the system_prompt bytes to it (utf-8), append `["--system-prompt-file", str(path)]` to cmd, and track the path in a local `sysprompt_path` variable. Wrap the existing `async with self._global_rate_limiter.concurrency_slot():` body so that on EVERY exit path (success, non-zero returncode, asyncio.TimeoutError, OSError, ProviderError, GeneratorExit, CancelledError) the file is removed via `try: sysprompt_path.unlink(missing_ok=True) except OSError: pass` in a finally block. Update the CLAUDE_CODE_STREAM log line to: `logger.info("CLAUDE_CODE_STREAM:{} model={} prompt_chars={} system_prompt_chars={} cmd={}", req_tag, cli_model, len(prompt), len(system_prompt), shlex.join(cmd))` — the joined cmd now contains only flags and the file path, never the system prompt text. Add to tests/unit/test_claude_code_sysprompt_file.py: test_system_prompt_travels_via_file_never_argv (capture spawn argv + the sysprompt file path; assert argv contains --system-prompt-file and the path, the system prompt text appears in NO argv element, and the file's bytes equal the system prompt at spawn time), test_sysprompt_file_removed_after_completion (after the stream ends, the captured sysprompt file no longer exists on disk), test_no_sysprompt_flag_when_absent (request with empty system prompt → argv has no --system-prompt* token and no file was written under self._workdir), test_log_line_reports_system_prompt_chars_not_text (capture the CLAUDE_CODE_STREAM log line and assert it contains system_prompt_chars=<int> and does NOT contain the system prompt text). Use the fake-exec capture pattern from the existing tools-emulation test.
- **Commit**: `feat(claude_code): ship system prompt via --system-prompt-file, scrub from log`
- **Done when**: pytest tests/unit/test_claude_code_sysprompt_file.py passes
- **Unit tests**: `tests/unit/test_claude_code_sysprompt_file.py::test_system_prompt_travels_via_file_never_argv`, `tests/unit/test_claude_code_sysprompt_file.py::test_sysprompt_file_removed_after_completion`, `tests/unit/test_claude_code_sysprompt_file.py::test_no_sysprompt_flag_when_absent`, `tests/unit/test_claude_code_sysprompt_file.py::test_log_line_reports_system_prompt_chars_not_text`

## Step 3 — AC2: real-spawn integration test for oversized system prompt + concurrent-file unit test

- **Files**: `tests/unit/test_claude_code_sysprompt_file.py`, `tests/integration/test_claude_code_sysprompt_e2big.py`
- **Action**: Append test_concurrent_requests_get_distinct_sysprompt_files to tests/unit/test_claude_code_sysprompt_file.py: drive two concurrent stream_response calls (asyncio.gather) each with a distinct system prompt, capture both spawn argv lists, assert each argv carries a different --system-prompt-file path and each file's bytes equal its own system prompt. Create tests/integration/test_claude_code_sysprompt_e2big.py with test_oversized_system_prompt_survives_real_spawn: write a tiny executable shell script under the test's tmp_path that drains stdin to /dev/null (the provider pipes the main prompt into it), writes its argv (one element per line) to a capture file and prints a valid `--output-format json` payload (`{"result": "ok", "usage": {"output_tokens": 1}}`) to stdout; construct ClaudeCodeProvider(cli_path=<that script>, subprocess_timeout=30.0); call stream_response with a 200_000-character system prompt; collect the SSE events; assert the stream completes without raising, the capture file's argv contains --system-prompt-file and a path under the provider's workdir, and the system prompt text does NOT appear in any argv line. This is the regression pin for the E2BIG class — it fails on the pre-fix code with OSError: [Errno 7].
- **Commit**: `test(claude_code): pin oversized system prompt via real subprocess spawn`
- **Done when**: pytest tests/integration/test_claude_code_sysprompt_e2big.py::test_oversized_system_prompt_survives_real_spawn passes
- **Unit tests**: `tests/unit/test_claude_code_sysprompt_file.py::test_concurrent_requests_get_distinct_sysprompt_files`

## Integration tests

- `tests/integration/test_claude_code_sysprompt_e2big.py::test_oversized_system_prompt_survives_real_spawn`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-CC-SYSPROMPT-FILE",
  "title": "claude_code system prompt travels via file, never argv",
  "summary": "Replace the argv-borne --system-prompt in ClaudeCodeProvider.stream_response with a per-request file under self._workdir passed via --system-prompt-file, clean it up on every exit path, scrub the system prompt from the CLAUDE_CODE_STREAM log line, and emit a loud init-time warning when shutil.which cannot resolve cli_path. Pins the fix with a new unit suite (argv/file/log/which) and a real-spawn integration test that drives a 200 KiB system prompt through the provider.",
  "steps": [
    {
      "index": 1,
      "title": "G3: loud init warning when cli_path is unresolvable + first unit test",
      "files": [
        "src/ferova/llm_proxy/providers/claude_code/client.py",
        "tests/unit/test_claude_code_sysprompt_file.py"
      ],
      "action": "In ClaudeCodeProvider.__init__, after the existing `resolved_cli = shutil.which(cli_path) or cli_path` line, detect the unresolvable case (`shutil.which(cli_path) is None`) and emit a single `logger.warning(\"CLAUDE_CODE_CLI_UNRESOLVABLE: cli_path={!r} not found on PATH; subprocess spawns will fail with OSError\", cli_path)` BEFORE assigning self._cli_path. Keep the existing fallback assignment so behaviour is unchanged. Create tests/unit/test_claude_code_sysprompt_file.py with a module-level helper that builds a provider with a given cli_path and captures loguru output via a temporary sink (`logger.add` appending records to a list, removed after the test — pytest's caplog does not capture loguru); add test_which_failure_logs_loud_warning that constructs the provider with cli_path=\"definitely-not-on-path-xyz\" and asserts the warning text contains the requested cli_path and the substring CLAUDE_CODE_CLI_UNRESOLVABLE. Use the same fake-exec capture pattern as tests/unit/test_claude_code_tools_emulation.py (patch asyncio.create_subprocess_exec) so no real binary is needed.",
      "commit_message": "feat(claude_code): warn loudly when cli_path is unresolvable at init",
      "done_when": "pytest tests/unit/test_claude_code_sysprompt_file.py::test_which_failure_logs_loud_warning passes",
      "unit_tests": [
        "tests/unit/test_claude_code_sysprompt_file.py::test_which_failure_logs_loud_warning"
      ]
    },
    {
      "index": 2,
      "title": "G1+G2: system prompt travels via --system-prompt-file, log line scrubs text",
      "files": [
        "src/ferova/llm_proxy/providers/claude_code/client.py",
        "tests/unit/test_claude_code_sysprompt_file.py"
      ],
      "action": "In ClaudeCodeProvider.stream_response, replace the `if system_prompt: cmd += [\"--system-prompt\", system_prompt]` block with: when system_prompt is truthy, generate a unique filename via `self._workdir / f\"sysprompt_{uuid.uuid4().hex}.txt\"`, write the system_prompt bytes to it (utf-8), append `[\"--system-prompt-file\", str(path)]` to cmd, and track the path in a local `sysprompt_path` variable. Wrap the existing `async with self._global_rate_limiter.concurrency_slot():` body so that on EVERY exit path (success, non-zero returncode, asyncio.TimeoutError, OSError, ProviderError, GeneratorExit, CancelledError) the file is removed via `try: sysprompt_path.unlink(missing_ok=True) except OSError: pass` in a finally block. Update the CLAUDE_CODE_STREAM log line to: `logger.info(\"CLAUDE_CODE_STREAM:{} model={} prompt_chars={} system_prompt_chars={} cmd={}\", req_tag, cli_model, len(prompt), len(system_prompt), shlex.join(cmd))` — the joined cmd now contains only flags and the file path, never the system prompt text. Add to tests/unit/test_claude_code_sysprompt_file.py: test_system_prompt_travels_via_file_never_argv (capture spawn argv + the sysprompt file path; assert argv contains --system-prompt-file and the path, the system prompt text appears in NO argv element, and the file's bytes equal the system prompt at spawn time), test_sysprompt_file_removed_after_completion (after the stream ends, the captured sysprompt file no longer exists on disk), test_no_sysprompt_flag_when_absent (request with empty system prompt → argv has no --system-prompt* token and no file was written under self._workdir), test_log_line_reports_system_prompt_chars_not_text (capture the CLAUDE_CODE_STREAM log line and assert it contains system_prompt_chars=<int> and does NOT contain the system prompt text). Use the fake-exec capture pattern from the existing tools-emulation test.",
      "commit_message": "feat(claude_code): ship system prompt via --system-prompt-file, scrub from log",
      "done_when": "pytest tests/unit/test_claude_code_sysprompt_file.py passes",
      "unit_tests": [
        "tests/unit/test_claude_code_sysprompt_file.py::test_system_prompt_travels_via_file_never_argv",
        "tests/unit/test_claude_code_sysprompt_file.py::test_sysprompt_file_removed_after_completion",
        "tests/unit/test_claude_code_sysprompt_file.py::test_no_sysprompt_flag_when_absent",
        "tests/unit/test_claude_code_sysprompt_file.py::test_log_line_reports_system_prompt_chars_not_text"
      ]
    },
    {
      "index": 3,
      "title": "AC2: real-spawn integration test for oversized system prompt + concurrent-file unit test",
      "files": [
        "tests/unit/test_claude_code_sysprompt_file.py",
        "tests/integration/test_claude_code_sysprompt_e2big.py"
      ],
      "action": "Append test_concurrent_requests_get_distinct_sysprompt_files to tests/unit/test_claude_code_sysprompt_file.py: drive two concurrent stream_response calls (asyncio.gather) each with a distinct system prompt, capture both spawn argv lists, assert each argv carries a different --system-prompt-file path and each file's bytes equal its own system prompt. Create tests/integration/test_claude_code_sysprompt_e2big.py with test_oversized_system_prompt_survives_real_spawn: write a tiny executable shell script under the test's tmp_path that drains stdin to /dev/null (the provider pipes the main prompt into it), writes its argv (one element per line) to a capture file and prints a valid `--output-format json` payload (`{\"result\": \"ok\", \"usage\": {\"output_tokens\": 1}}`) to stdout; construct ClaudeCodeProvider(cli_path=<that script>, subprocess_timeout=30.0); call stream_response with a 200_000-character system prompt; collect the SSE events; assert the stream completes without raising, the capture file's argv contains --system-prompt-file and a path under the provider's workdir, and the system prompt text does NOT appear in any argv line. This is the regression pin for the E2BIG class — it fails on the pre-fix code with OSError: [Errno 7].",
      "commit_message": "test(claude_code): pin oversized system prompt via real subprocess spawn",
      "done_when": "pytest tests/integration/test_claude_code_sysprompt_e2big.py::test_oversized_system_prompt_survives_real_spawn passes",
      "unit_tests": [
        "tests/unit/test_claude_code_sysprompt_file.py::test_concurrent_requests_get_distinct_sysprompt_files"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_claude_code_sysprompt_e2big.py::test_oversized_system_prompt_survives_real_spawn"
  ]
}
```
