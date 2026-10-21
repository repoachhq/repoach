"""``scripts/release_notes.py``: notes grouped by Conventional Commit type (SP-PYPI-RELEASE G4)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release_notes.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_notes", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_notes"] = module
    spec.loader.exec_module(module)
    return module


rn = _load()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def _commit(repo: Path, message: str) -> None:
    _git(repo, "commit", "--allow-empty", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "chore: genesis")
    _git(tmp_path, "tag", "v0.0.1")
    _commit(tmp_path, "feat(cli): add the doctor command (#12)")
    _commit(tmp_path, "fix(review): keep the head tree")
    _commit(tmp_path, "docs: say what repoach is")
    _commit(tmp_path, "perf(arch): cache the spec index")
    _commit(tmp_path, "refactor(forge): extract the protocol")
    _commit(tmp_path, "feat(api)!: drop the v1 routes")
    _commit(tmp_path, "fix(config): rename the env file\n\nBREAKING CHANGE: .env moves")
    _commit(tmp_path, "ci: pin the runner")
    _commit(tmp_path, "Update README")
    _git(tmp_path, "checkout", "-q", "-b", "side")
    _commit(tmp_path, "test(cli): cover the side branch")
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "merge", "--no-ff", "-q", "-m", "Merge branch 'side'", "side")
    _git(tmp_path, "tag", "v0.1.0")
    return tmp_path


def _run(repo: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = rn.main([*args, "--repo", str(repo)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_heading_then_features_when_nothing_breaks(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(repo, "tag", "v0.0.2", "HEAD~7")
    code, out, _ = _run(repo, "v0.0.1", "v0.0.2", capsys=capsys)
    assert code == 0
    assert out.splitlines()[:3] == ["## v0.0.2", "", "### Features"]


def test_breaking_changes_come_first_and_sections_follow_the_fixed_order(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _run(repo, "v0.0.1", "v0.1.0", capsys=capsys)
    assert code == 0
    headings = [line for line in out.splitlines() if line.startswith("#")]
    assert headings == [
        "## v0.1.0",
        "### Breaking changes",
        "### Features",
        "### Fixes",
        "### Performance",
        "### Refactoring",
        "### Documentation",
        "### Other",
    ]
    breaking = out.split("### Breaking changes")[1].split("###")[0]
    assert "**api:** drop the v1 routes" in breaking
    assert "**config:** rename the env file" in breaking
    features = out.split("### Features")[1].split("###")[0]
    assert "- **cli:** add the doctor command (#12) (" in features
    other = out.split("### Other")[1]
    assert "pin the runner" in other
    assert "Update README" in other
    assert "cover the side branch" in other


def test_merge_commits_and_the_previous_range_are_left_out(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _run(repo, "v0.0.1", "v0.1.0", capsys=capsys)
    assert "Merge branch" not in out
    assert "genesis" not in out


def test_missing_ref_exits_2(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(repo, "v9.9.9", "v0.1.0", capsys=capsys)
    assert code == 2
    assert out == ""
    assert "no such ref: v9.9.9" in err


def test_empty_range_says_so(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _run(repo, "v0.1.0", "v0.1.0", capsys=capsys)
    assert code == 0
    assert out == "## v0.1.0\n\nNo changes.\n"


def test_parse_commit_reads_scope_bang_and_footer() -> None:
    plain = rn.parse_commit("a" * 40, "fix(cli): say it", "")
    assert (plain.type, plain.scope, plain.subject, plain.breaking) == (
        "fix",
        "cli",
        "say it",
        False,
    )
    assert rn.parse_commit("b" * 40, "feat!: go", "").breaking
    assert rn.parse_commit("c" * 40, "feat: go", "BREAKING-CHANGE: x").breaking
    loose = rn.parse_commit("d" * 40, "Merge stuff", "")
    assert (loose.type, loose.subject) == ("", "Merge stuff")
