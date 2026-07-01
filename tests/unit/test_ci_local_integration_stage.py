"""Contract tests for the integration stage in ci_local.sh.

Textual contract checks, matching the repo's other script-gate tests
(they read the shell source rather than spawning it, keeping the suite
fast and hermetic). Pins: the integration stage exists, the
``--integration`` flag is declared, the stage is gated out of
``--fast`` (lint-only) mode, and the empty-directory skip the spec
requires is present so an empty ``tests/integration/`` never reds CI.
"""

from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci_local.sh"


def _lint_block() -> str:
    """Return the body of the ``--fast`` (lint-only) conditional block."""
    lines = SCRIPT_PATH.read_text().splitlines()
    body: list[str] = []
    collecting = False
    for line in lines:
        if 'if [[ "$mode" != "tests" && "$mode" != "integration" ]]' in line:
            collecting = True
            continue
        if collecting:
            if line.strip() == "fi":
                break
            body.append(line)
    return "\n".join(body)


def test_integration_stage_present() -> None:
    assert "tests/integration" in SCRIPT_PATH.read_text()


def test_integration_flag_declared() -> None:
    assert "--integration" in SCRIPT_PATH.read_text()


def test_fast_mode_excludes_integration_stage() -> None:
    assert "tests/integration" not in _lint_block()


def test_empty_integration_dir_is_skipped_not_failed() -> None:
    content = SCRIPT_PATH.read_text()
    assert "find tests/integration -name 'test_*.py'" in content
    assert "stage skipped" in content
