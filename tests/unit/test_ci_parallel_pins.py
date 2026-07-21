"""Contract tests for the pytest-xdist parallelisation pins.

Textual contract checks, matching the repo's other script-gate tests
(they read configuration sources rather than spawning tools, keeping
the suite fast and hermetic).  Pins: pytest-xdist is a declared dev
dependency, and addopts stays serial-neutral so selector-sized runs
never pay worker-startup cost.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_xdist_is_a_dev_dependency() -> None:
    """Parse pyproject.toml and assert any dev-extras entry starts with ``pytest-xdist``."""
    with PYPROJECT_PATH.open("rb") as fh:
        data = tomllib.load(fh)

    dev_extras: list[str] = data["project"]["optional-dependencies"]["dev"]
    assert any(entry.startswith("pytest-xdist") for entry in dev_extras), (
        f"pytest-xdist not found in dev extras: {dev_extras}"
    )


def test_addopts_stays_serial_neutral() -> None:
    """Assert ``-n`` is absent from ``tool.pytest.ini_options.addopts``."""
    with PYPROJECT_PATH.open("rb") as fh:
        data = tomllib.load(fh)

    addopts: str = data["tool"]["pytest"]["ini_options"]["addopts"]
    assert "-n" not in addopts, f"addopts must not contain -n: {addopts!r}"


def test_ci_local_full_suite_runs_parallel() -> None:
    """Every full-suite pytest invocation in ci_local.sh carries -n auto --dist worksteal."""
    ci_path = Path(__file__).resolve().parents[2] / "scripts" / "ci_local.sh"
    lines = ci_path.read_text().splitlines()

    invocation_lines = [
        ln.strip()
        for ln in lines
        if "python -m pytest" in ln and ("tests/unit" in ln or "tests/integration" in ln)
    ]
    assert invocation_lines, "no full-suite pytest invocations found in ci_local.sh"
    for ln in invocation_lines:
        assert "-n auto" in ln, f"ci_local.sh invocation missing -n auto: {ln!r}"
        assert "--dist worksteal" in ln, f"ci_local.sh invocation missing --dist worksteal: {ln!r}"
