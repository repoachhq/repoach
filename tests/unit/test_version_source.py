"""SP-PYPI-RELEASE G1: the git tag is the only source of the version."""

from __future__ import annotations

import ast
import tomllib
from importlib import metadata
from pathlib import Path

import repoach

_ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> dict:
    return tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_runtime_version_is_the_installed_metadata() -> None:
    assert repoach.__version__ == metadata.version("repoach")


def test_pyproject_declares_no_literal_version() -> None:
    project = _pyproject()["project"]
    assert "version" not in project
    assert "version" in project["dynamic"]


def test_version_comes_from_the_vcs_through_hatch() -> None:
    data = _pyproject()
    assert "hatch-vcs>=0.4" in data["build-system"]["requires"]
    assert data["tool"]["hatch"]["version"]["source"] == "vcs"
    assert data["tool"]["hatch"]["version"]["raw-options"]["version_scheme"] == (
        "release-branch-semver"
    )


def test_package_init_holds_no_version_literal() -> None:
    tree = ast.parse((_ROOT / "src" / "repoach" / "__init__.py").read_text(encoding="utf-8"))
    literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
        and isinstance(node.value, ast.Constant)
        and node.value.value != "0+unknown"
    ]
    assert literals == []
