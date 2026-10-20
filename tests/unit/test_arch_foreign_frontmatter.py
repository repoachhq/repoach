"""``arch check`` on a foreign repository whose specs carry their own frontmatter.

SP-ARCH-FOREIGN-FRONTMATTER. Measured 2026-10-20 on Yoob (whose
``repoach.toml`` sets ``[arch].require_frontmatter = false``):
``repoach arch check --base origin/develop`` raised
``MalformedFrontmatterError: 2026-10-18_editor-templates-show-declared-durations.md:
missing frontmatter keys`` as a traceback, rc 1. Each test builds a real
scratch git repository holding such a human spec.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoach.arch import MalformedFrontmatterError, load_registry
from repoach.cli.main import app
from repoach.lint import edge_honesty

_HUMAN_SPEC = "2026-10-18_editor-templates-show-declared-durations.md"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _foreign_repo(tmp_path: Path, *, require_frontmatter: bool) -> Path:
    repo = tmp_path / "foreign"
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / "src").mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    flag = "true" if require_frontmatter else "false"
    (repo / "repoach.toml").write_text(f"[arch]\nrequire_frontmatter = {flag}\n")
    (repo / "src" / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / "docs" / "specs" / _HUMAN_SPEC).write_text(
        "---\ntitle: Editor templates show declared durations\nstatus: draft\n---\n\n# Body\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "spec")
    return repo


def test_registry_raises_by_default_and_collects_when_tolerant(tmp_path: Path) -> None:
    specs = _foreign_repo(tmp_path, require_frontmatter=False) / "docs" / "specs"
    with pytest.raises(MalformedFrontmatterError):
        load_registry(specs)
    registry = load_registry(specs, tolerate_malformed=True)
    assert [m.path.name for m in registry.malformed] == [_HUMAN_SPEC]
    assert "missing frontmatter keys" in registry.malformed[0].reason


def test_not_required_lists_the_spec_as_ungoverned(tmp_path: Path) -> None:
    repo = _foreign_repo(tmp_path, require_frontmatter=False)
    report = edge_honesty.run(
        base="develop~1",
        staged=False,
        specs_dir=repo / "docs" / "specs",
        repo_root=repo,
        require_frontmatter=False,
    )
    assert report.ok
    assert [s.path.name for s in report.ungoverned_specs] == [_HUMAN_SPEC]
    assert report.malformed_specs == ()
    assert report.spec_violations == ()


def test_required_names_the_violation(tmp_path: Path) -> None:
    repo = _foreign_repo(tmp_path, require_frontmatter=True)
    report = edge_honesty.run(
        base="develop~1",
        staged=False,
        specs_dir=repo / "docs" / "specs",
        repo_root=repo,
        require_frontmatter=True,
    )
    assert not report.ok
    assert [s.path.name for s in report.malformed_specs] == [_HUMAN_SPEC]
    assert any(
        line.startswith(f"malformed spec: {_HUMAN_SPEC}")
        for line in edge_honesty.report_lines(report)
    )


@pytest.mark.parametrize(
    ("require_frontmatter", "expected_rc", "expected_line"),
    [
        (False, 0, "ungoverned: 1 spec(s) without registry frontmatter"),
        (True, 1, f"malformed spec: {_HUMAN_SPEC}: missing frontmatter keys"),
    ],
)
def test_arch_check_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_frontmatter: bool,
    expected_rc: int,
    expected_line: str,
) -> None:
    repo = _foreign_repo(tmp_path, require_frontmatter=require_frontmatter)
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["arch", "check", "--base", "develop~1"])
    assert result.exit_code == expected_rc, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert expected_line in result.stderr
    assert "Traceback" not in result.output
