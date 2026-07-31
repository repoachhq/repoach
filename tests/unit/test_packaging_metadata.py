"""Guard tests for the distribution metadata in `pyproject.toml`.

repoach ships as a public PyPI package, so its `[project]` table must carry the
metadata PyPI needs to render and index the project: an SPDX license, a bundled
license file, discovery keywords, and trove classifiers whose declared Python
versions stay consistent with `requires-python`. These parse the manifest and
lock that surface so a future edit cannot silently strip it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _project() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]


def test_license_is_spdx_mit_with_bundled_file() -> None:
    project = _project()
    assert project["license"] == "MIT"
    assert "LICENSE" in project["license-files"]
    assert (_PYPROJECT.parent / "LICENSE").is_file()


def test_keywords_and_classifiers_are_populated() -> None:
    project = _project()
    assert len(project["keywords"]) >= 3
    classifiers = project["classifiers"]
    assert any(c.startswith("Development Status ::") for c in classifiers)
    assert any(c.startswith("Intended Audience ::") for c in classifiers)


def test_python_classifiers_cover_the_supported_range() -> None:
    project = _project()
    declared = {
        c.rsplit("::", 1)[1].strip()
        for c in project["classifiers"]
        if c.startswith("Programming Language :: Python :: 3.")
    }
    assert {"3.11", "3.12", "3.13"} <= declared
    floor = project["requires-python"].lstrip(">=").strip()
    assert floor in declared
