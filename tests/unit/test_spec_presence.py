"""Tests for the spec-presence gate (SP-ARCH-SPEC-PRESENCE, slice C).

A newly-ADDED spec without a frontmatter fence must fail the edge-honesty
gate (else it becomes an un-graphed frontier node silently). A modified
legacy spec and the template are not flagged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ferova.arch import has_frontmatter
from ferova.lint.edge_honesty import (
    Report,
    SpecPresenceViolation,
    _is_pre_template,
    check_added_specs,
    gather_added_specs,
    report_lines,
)

_FENCED = "---\nid: SP-X\ntitle: X\nversion: 0.1\nstatus: draft\nowns:\n  code: []\n  resources: N/A\ndepends_on: []\n---\n\n# X\n"
_FENCELESS = "# Legacy spec\n\nNo frontmatter here.\n"


def test_has_frontmatter_true_and_false() -> None:
    assert has_frontmatter(_FENCED) is True
    assert has_frontmatter(_FENCELESS) is False


def test_check_added_specs_flags_fenceless(tmp_path: Path) -> None:
    specs = tmp_path / "docs" / "specs"
    specs.mkdir(parents=True)
    (specs / "01_SP-GOOD_g.md").write_text(_FENCED, encoding="utf-8")
    (specs / "01_SP-BAD_b.md").write_text(_FENCELESS, encoding="utf-8")

    violations = check_added_specs(
        ["docs/specs/01_SP-GOOD_g.md", "docs/specs/01_SP-BAD_b.md"], tmp_path
    )
    assert violations == [SpecPresenceViolation(path="docs/specs/01_SP-BAD_b.md")]


def test_report_not_ok_on_spec_violation() -> None:
    report = Report(
        violations=(),
        frontier=(),
        spec_violations=(SpecPresenceViolation(path="docs/specs/x.md"),),
    )
    assert report.ok is False
    assert any("ungoverned new spec" in line for line in report_lines(report))


def test_grandfather_pre_template_specs() -> None:
    assert _is_pre_template("2026-06-18_SP-OLD_x.md") is True
    assert _is_pre_template("2026-06-20_SP-OLD_x.md") is True
    assert _is_pre_template("2026-06-21_SP-NEW_x.md") is False
    assert _is_pre_template("2026-07-01_SP-NEW_x.md") is False
    assert _is_pre_template("SP-NODATE_x.md") is False


def test_gather_added_specs_grandfathers_pre_template(tmp_path: Path) -> None:
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    specs = repo / "docs" / "specs"
    specs.mkdir(parents=True)
    (specs / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    (specs / "2026-06-18_SP-OLD_o.md").write_text(_FENCELESS, encoding="utf-8")
    (specs / "2026-06-21_SP-NEW_n.md").write_text(_FENCELESS, encoding="utf-8")
    _git(repo, "add", "-A")

    added = gather_added_specs(base="HEAD", staged=True, repo_root=repo)
    assert added == ["docs/specs/2026-06-21_SP-NEW_n.md"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_gather_added_specs_added_only_and_filtered(tmp_path: Path) -> None:
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    specs = repo / "docs" / "specs"
    specs.mkdir(parents=True)
    (specs / "01_SP-OLD_o.md").write_text(_FENCELESS, encoding="utf-8")
    (specs / "_TEMPLATE.md").write_text(_FENCED, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    (specs / "02_SP-NEW_n.md").write_text(_FENCELESS, encoding="utf-8")
    (specs / "01_SP-OLD_o.md").write_text(_FENCELESS + "edit\n", encoding="utf-8")
    (specs / "_TEMPLATE2.md").write_text(_FENCELESS, encoding="utf-8")
    _git(repo, "add", "-A")

    added = gather_added_specs(base="HEAD", staged=True, repo_root=repo)

    assert added == ["docs/specs/02_SP-NEW_n.md"]
