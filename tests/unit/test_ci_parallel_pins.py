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
