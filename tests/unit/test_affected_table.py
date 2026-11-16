"""The path-to-job table and its one matching rule (SP-AFFECTED-JOBS-TABLE T1, T2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoach.affected.select import DEFAULT_WORKFLOW_PATH, workflow_jobs
from repoach.affected.table import (
    DEFAULT_TABLE_PATH,
    TABLE_VERSION,
    Row,
    TableError,
    load_table,
    matches,
    matching_entry,
)

_ROOT = Path(__file__).resolve().parents[2]
_JOBS = ("test", "wheel-build", "wheel-smoke")

_VALID = """\
version = 1

[gate]
pytest = ["tests/unit/test_a.py"]

[rows.docs]
prefixes = ["docs/"]
jobs = []
reason = "pages"

[rows.wheel]
prefixes = ["src/"]
suffixes = [".sh"]
jobs = ["wheel-build", "wheel-smoke"]
reason = "what the wheel packages"

[full]
prefixes = [".github/"]
reason = "everything runs"
"""


def _row(*, prefixes: tuple[str, ...] = (), suffixes: tuple[str, ...] = ()) -> Row:
    return Row(name="r", prefixes=prefixes, suffixes=suffixes, reason="why")


@pytest.mark.parametrize(
    ("prefixes", "suffixes", "path", "expected"),
    [
        (("docs/",), (), "docs/a.md", True),
        (("docs",), (), "docs/a.md", False),
        (("LICENSE",), (), "LICENSE", True),
        (("LICENSE",), (), "LICENSES", False),
        ((), (".sh",), "scripts/x.sh", True),
        (("src/",), (), "tests/src/x.py", False),
        (("docs/",), (), "docs", False),
        ((), (".sh",), "scripts/x.shx", False),
    ],
)
def test_matches_prefix_and_suffix_only(
    prefixes: tuple[str, ...], suffixes: tuple[str, ...], path: str, expected: bool
) -> None:
    assert matches(_row(prefixes=prefixes, suffixes=suffixes), path) is expected


def test_matching_entry_names_the_entry_that_matched() -> None:
    row = _row(prefixes=("docs/", "README.md"), suffixes=(".sh",))
    assert matching_entry(row, "README.md") == "README.md"
    assert matching_entry(row, "docs/x.sh") == "docs/"
    assert matching_entry(row, "scripts/x.sh") == ".sh"
    assert matching_entry(row, "src/a.py") is None


def test_load_the_committed_table() -> None:
    workflow = _ROOT / DEFAULT_WORKFLOW_PATH
    table = load_table(_ROOT / DEFAULT_TABLE_PATH, workflow_jobs(workflow), workflow=workflow.name)
    assert table.version == TABLE_VERSION
    assert [row.name for row in table.rows] == ["docs", "python", "wheel"]
    assert table.full.jobs == frozenset()
    assert table.gate_pytest


def test_load_a_valid_table(tmp_path: Path) -> None:
    path = tmp_path / "affected-jobs.toml"
    path.write_text(_VALID, encoding="utf-8")
    table = load_table(path, _JOBS, workflow="ci.yml")
    assert table.gate_pytest == ("tests/unit/test_a.py",)
    assert table.rows[1].suffixes == (".sh",)
    assert table.rows[1].jobs == frozenset({"wheel-build", "wheel-smoke"})
    assert table.full.prefixes == (".github/",)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            'jobs = ["wheel-build", "wheel-smoke"]',
            'jobs = ["wheel-build", "wheel-smok"]',
            "affected-jobs.toml: rows.wheel.jobs names 'wheel-smok', not a job of ci.yml",
        ),
        ('reason = "pages"\n', "", "affected-jobs.toml: rows.docs.reason is missing or empty"),
        ("version = 1", "version = 2", "affected-jobs.toml: version is 2, this reader reads 1"),
        ("jobs = []\n", "", "affected-jobs.toml: rows.docs.jobs is missing"),
        (
            'prefixes = ["docs/"]\n',
            "",
            "affected-jobs.toml: rows.docs has neither prefixes nor suffixes",
        ),
        (
            'prefixes = ["docs/"]',
            'globs = ["docs/**"]',
            "affected-jobs.toml: rows.docs unknown key 'globs'",
        ),
        (
            "[rows.docs]",
            "[rows.unknown]",
            "affected-jobs.toml: rows.unknown is a reserved row name",
        ),
        (
            'reason = "everything runs"',
            'reason = "x"\njobs = ["test"]',
            "affected-jobs.toml: full unknown key 'jobs'",
        ),
        ("[full]", "[fulls]", "affected-jobs.toml: fulls is not a key of the format"),
        (
            'prefixes = ["docs/"]',
            "prefixes = [1]",
            "affected-jobs.toml: rows.docs.prefixes holds 1, not a non-empty string",
        ),
    ],
)
def test_load_table_names_the_key_of_every_defect(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    assert old in _VALID
    path = tmp_path / "affected-jobs.toml"
    path.write_text(_VALID.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(TableError) as caught:
        load_table(path, _JOBS, workflow="ci.yml")
    assert str(caught.value) == message


def test_load_table_without_full(tmp_path: Path) -> None:
    path = tmp_path / "affected-jobs.toml"
    path.write_text(_VALID.split("[full]")[0], encoding="utf-8")
    with pytest.raises(TableError, match="full is missing"):
        load_table(path, _JOBS, workflow="ci.yml")


def test_load_table_unreadable_and_not_toml(tmp_path: Path) -> None:
    with pytest.raises(TableError, match="cannot be read"):
        load_table(tmp_path / "absent.toml", _JOBS, workflow="ci.yml")
    path = tmp_path / "affected-jobs.toml"
    path.write_text("version = = 1\n", encoding="utf-8")
    with pytest.raises(TableError, match="is not TOML"):
        load_table(path, _JOBS, workflow="ci.yml")
