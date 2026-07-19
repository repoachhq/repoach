"""SP-CC-SYSPROMPT-FILE — AC2 real-spawn integration test.

A 200 000-character system prompt must survive a real subprocess
spawn of a minimal executable stand-in for the ``claude`` CLI.  This
is the regression pin for the ``OSError: [Errno 7]`` class — it fails
on the pre-fix code because the system prompt cannot fit in argv.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.claude_code.client import ClaudeCodeProvider


def test_oversized_system_prompt_survives_real_spawn(tmp_path: Path) -> None:
    """A 200 000-character system prompt survives a real subprocess spawn:
    the stream completes without raising, the captured argv carries
    ``--system-prompt-file`` with a path under the provider's workdir,
    and the system prompt text is absent from every argv line."""
    capture_file = tmp_path / "argv_capture.txt"
    script_path = tmp_path / "fake_claude.sh"

    script_content = f"""#!/bin/sh
cat > /dev/null
for arg in "$@"; do
    printf '%s\\n' "$arg"
done > "{capture_file}"
printf '{{"result": "ok", "usage": {{"output_tokens": 1}}}}\\n'
"""
    script_path.write_text(script_content)
    script_path.chmod(0o755)

    system_prompt = "X" * 200_000

    provider = ClaudeCodeProvider(
        ProviderConfig(api_key="unused"),
        cli_path=str(script_path),
        subprocess_timeout=30.0,
    )

    request = SimpleNamespace(
        model="claude-sonnet-4-6",
        system=system_prompt,
        messages=[{"role": "user", "content": "Hello."}],
        tools=None,
    )

    async def drive() -> list[str]:
        events: list[str] = []
        async for event in provider.stream_response(request):
            events.append(event)
        return events

    events = asyncio.run(drive())

    assert len(events) > 0, "Expected SSE events from stream_response, got none"

    assert capture_file.exists(), f"Capture file {capture_file} was not created by the subprocess"
    argv_lines = capture_file.read_text().strip().split("\n")

    assert "--system-prompt-file" in argv_lines, (
        f"Expected --system-prompt-file in argv, got {argv_lines}"
    )

    spf_idx = argv_lines.index("--system-prompt-file")
    assert spf_idx + 1 < len(argv_lines), (
        "--system-prompt-file was the last argv element; missing path"
    )
    sysprompt_path_str = argv_lines[spf_idx + 1]

    assert sysprompt_path_str.startswith(str(provider._workdir)), (
        f"sysprompt path {sysprompt_path_str!r} not under workdir {provider._workdir}"
    )

    assert not any(system_prompt in line for line in argv_lines), (
        "System prompt text must not appear in any argv element"
    )
