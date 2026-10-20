"""SP-UNIVERSAL-CI-ENTRY: ``repoach init --ci``.

The rendered jobs are parsed as YAML, never matched as text alone; the
init cases run on scratch git repositories shaped like a Go project
(gum) and like Yoob (an existing ``.gitlab-ci.yml`` that must stay
byte-identical).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from repoach.ci_entry.snippets import (
    GITHUB_WORKFLOW_PATH,
    GITLAB_JOB_NAME,
    GITLAB_JOB_PATH,
    InstallTarget,
    render_github_workflow,
    render_gitlab_job,
)
from repoach.cli.main import app

SECRETS = (
    "GITLAB_TOKEN",
    "REPOACH_GITLAB_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "REPOACH_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "REPOACH_ANTHROPIC_AUTH_TOKEN",
)


def _unset_names(command: str) -> set[str]:
    words = command.split()
    return {words[i + 1] for i, word in enumerate(words) if word == "-u"}


class TestRender:
    def test_gitlab_job_shape(self) -> None:
        job = yaml.safe_load(render_gitlab_job(InstallTarget(ref="v0.2.0")))[GITLAB_JOB_NAME]
        assert "merge_request_event" in job["rules"][0]["if"]
        assert job["allow_failure"] is True
        assert "tags" not in job
        assert job["variables"]["REPOACH_INSTALL"] == (
            "repoach @ git+https://github.com/repoachhq/repoach@v0.2.0"
        )
        assert job["variables"]["REPOACH_ADVISORY_POST"] == "0"
        install, review = job["script"][1], job["script"][2]
        assert set(SECRETS) <= _unset_names(install)
        assert review == "repoach ci review"

    def test_github_workflow_shape(self) -> None:
        doc = yaml.safe_load(render_github_workflow(InstallTarget(pypi_version="0.2.0")))
        on = doc[True] if True in doc else doc["on"]
        assert "opened" in on["pull_request"]["types"]
        job = doc["jobs"]["review"]
        assert job["continue-on-error"] is True
        install, review = job["steps"][2], job["steps"][3]
        assert install["env"] == {"REPOACH_INSTALL": "repoach==0.2.0"}
        assert set(SECRETS) <= _unset_names(install["run"])
        assert review["run"] == "repoach ci review"
        assert "REPOACH_ANTHROPIC_API_KEY" in review["env"]
        assert all(
            "@" in step["uses"] and len(step["uses"].split("@")[1]) == 40
            for step in job["steps"][:2]
        )


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


@pytest.fixture
def gum(tmp_path: Path) -> Path:
    repo = _git_repo(tmp_path / "gum")
    (repo / "go.mod").write_text("module github.com/charmbracelet/gum\n", encoding="utf-8")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "build.yml").write_text("name: build\n", encoding="utf-8")
    return repo


@pytest.fixture
def yoob(tmp_path: Path) -> Path:
    repo = _git_repo(tmp_path / "yoob")
    (repo / ".gitlab-ci.yml").write_text(
        "stages: [lint, review]\nreview:advisory:\n  script: [bash ci/review-advisory.sh]\n",
        encoding="utf-8",
    )
    return repo


def _init(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> str:
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["init", *args])
    assert result.exit_code == 0, result.output
    return result.output


class TestInitCi:
    def test_github_on_gum_is_idempotent(self, gum: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        build = (gum / ".github" / "workflows" / "build.yml").read_bytes()
        first = _init(gum, monkeypatch, "--ci", "github", "--ref", "v0.2.0")
        assert f"created {GITHUB_WORKFLOW_PATH.as_posix()}" in first
        written = (gum / GITHUB_WORKFLOW_PATH).read_bytes()
        second = _init(gum, monkeypatch, "--ci", "github", "--ref", "v0.2.0")
        assert f"unchanged {GITHUB_WORKFLOW_PATH.as_posix()}" in second
        assert (gum / GITHUB_WORKFLOW_PATH).read_bytes() == written
        assert (gum / ".github" / "workflows" / "build.yml").read_bytes() == build
        assert not (gum / "repoach.toml").exists()

    def test_gitlab_on_yoob_never_edits_the_pipeline(
        self, yoob: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipeline = (yoob / ".gitlab-ci.yml").read_bytes()
        out = _init(yoob, monkeypatch, "--ci", "gitlab")
        assert f"created {GITLAB_JOB_PATH.as_posix()}" in out
        assert f"- local: {GITLAB_JOB_PATH.as_posix()}" in out
        assert (yoob / ".gitlab-ci.yml").read_bytes() == pipeline
        job = yaml.safe_load((yoob / GITLAB_JOB_PATH).read_text(encoding="utf-8"))
        assert GITLAB_JOB_NAME in job

    def test_dry_run_and_print_write_nothing(
        self, yoob: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dry = _init(yoob, monkeypatch, "--ci", "gitlab", "--dry-run")
        assert f"would create {GITLAB_JOB_PATH.as_posix()}" in dry
        printed = _init(yoob, monkeypatch, "--ci", "github", "--print")
        assert "repoach ci review" in printed
        status = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=yoob,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert status.strip() == "?? .gitlab-ci.yml"

    def test_differing_file_is_kept_unless_force(
        self, yoob: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = yoob / GITLAB_JOB_PATH
        target.parent.mkdir(parents=True)
        target.write_text("hand-edited: true\n", encoding="utf-8")
        kept = _init(yoob, monkeypatch, "--ci", "gitlab")
        assert "differs" in kept
        assert target.read_text(encoding="utf-8") == "hand-edited: true\n"
        forced = _init(yoob, monkeypatch, "--ci", "gitlab", "--force")
        assert "overwritten" in forced
        assert GITLAB_JOB_NAME in yaml.safe_load(target.read_text(encoding="utf-8"))

    def test_unknown_ci_is_rc_2(self, yoob: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(yoob)
        result = CliRunner().invoke(app, ["init", "--ci", "jenkins"])
        assert result.exit_code == 2
