"""Unit tests for SP-PIN-TOOLCHAIN G1 — the dev extra pins ruff to one minor."""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import version
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
_SPEC = re.compile(r"^ruff>=(\d+)\.(\d+),<(\d+)\.(\d+)$")
_MINOR_PARTS = 2


def _declaration() -> str:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    dev = data["project"]["optional-dependencies"]["dev"]
    matches = [dep for dep in dev if re.match(r"^ruff(?![A-Za-z0-9._-])", dep)]
    assert len(matches) == 1, matches
    return matches[0]


def test_declared_range_spans_exactly_one_minor() -> None:
    match = _SPEC.match(_declaration())
    assert match, _declaration()
    low_major, low_minor, high_major, high_minor = (int(g) for g in match.groups())
    assert (high_major, high_minor) == (low_major, low_minor + 1)


def test_installed_ruff_is_inside_the_declared_range() -> None:
    declaration = _declaration()
    match = _SPEC.match(declaration)
    assert match, declaration
    low_major, low_minor = int(match.group(1)), int(match.group(2))
    installed = version("ruff")
    major, minor = (int(part) for part in installed.split(".")[:_MINOR_PARTS])
    assert (major, minor) == (low_major, low_minor), (
        f"installed ruff {installed} outside declared range {declaration.removeprefix('ruff')}"
    )
