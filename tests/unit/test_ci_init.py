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

from repoach.ci_entry.pin import InstallTarget, resolve_pin
from repoach.ci_entry.snippets import (
    GITHUB_WORKFLOW_PATH,
    GITLAB_JOB_NAME,
    GITLAB_JOB_PATH,
    render_github_workflow,
    render_gitlab_job,
)
from repoach.cli.main import app

GITLAB_CREDENTIALS: dict[str, str] = {
    "CI_REGISTRY_PASSWORD": "registry-secret",
    "CI_DEPENDENCY_PROXY_PASSWORD": "proxy-secret",
    "CI_JOB_JWT": "jwt-secret",
    "CI_JOB_JWT_V2": "jwt2-secret",
    "CI_JOB_TOKEN": "job-secret",
    "GITLAB_TOKEN": "glpat-secret",
    "REPOACH_GITLAB_TOKEN": "glpat-secret",
    "REPOACH_ANTHROPIC_API_KEY": "sk-ant-secret",
    "REPOACH_ANTHROPIC_AUTH_TOKEN": "gateway-secret",
    "PROJECT_MASKED_DB_PASSWORD": "db-secret",
}
"""What a GitLab job holds: the known names and one nobody can predict."""

GITHUB_CREDENTIALS: dict[str, str] = {
    "ACTIONS_RUNTIME_TOKEN": "runtime-secret",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-secret",
    "GITHUB_TOKEN": "ghs-secret",
    "GH_TOKEN": "ghs-secret",
    "REPOACH_ANTHROPIC_API_KEY": "sk-ant-secret",
    "REPOACH_ANTHROPIC_AUTH_TOKEN": "gateway-secret",
    "ORG_DEPLOY_KEY": "deploy-secret",
}


def _dump_tools(tmp_path: Path) -> tuple[Path, Path]:
    """Put fake ``python`` and ``apt-get`` first on PATH; each dumps its env."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    for name in ("python", "apt-get"):
        tool = bin_dir / name
        tool.write_text(f"#!/bin/sh\nenv > {dumps}/{name}.env\n", encoding="utf-8")
        tool.chmod(0o755)
    return bin_dir, dumps


def _run_step(command: str, env: dict[str, str]) -> None:
    subprocess.run(["bash", "-c", command], env=env, check=True)


def _assert_scrubbed(dump: Path, credentials: dict[str, str], *, keeps: str) -> None:
    text = dump.read_text(encoding="utf-8")
    names = {line.split("=", 1)[0] for line in text.splitlines() if "=" in line}
    assert names.isdisjoint(credentials), names & set(credentials)
    assert all(value not in text for value in credentials.values())
    assert keeps in names


class TestScrubbedSteps:
    def test_gitlab_apt_and_pip_see_no_credential(self, tmp_path: Path) -> None:
        job = yaml.safe_load(render_gitlab_job(InstallTarget(ref="v0.2.0")))[GITLAB_JOB_NAME]
        bin_dir, dumps = _dump_tools(tmp_path)
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "HTTPS_PROXY": "http://proxy.internal:3128",
            "GITLAB_CI": "true",
            "REPOACH_INSTALL": job["variables"]["REPOACH_INSTALL"],
            **GITLAB_CREDENTIALS,
        }
        _run_step(job["script"][0], env)
        _run_step(job["script"][1], env)
        _assert_scrubbed(dumps / "apt-get.env", GITLAB_CREDENTIALS, keeps="HTTPS_PROXY")
        _assert_scrubbed(dumps / "python.env", GITLAB_CREDENTIALS, keeps="HTTPS_PROXY")
        assert "DEBIAN_FRONTEND=noninteractive" in (dumps / "apt-get.env").read_text()
        assert "GITLAB_CI" not in (dumps / "python.env").read_text()
        assert job["script"][2] == "repoach ci review"

    def test_github_pip_sees_no_credential(self, tmp_path: Path) -> None:
        doc = yaml.safe_load(render_github_workflow(InstallTarget(pypi_version="0.2.0")))
        install = doc["jobs"]["review"]["steps"][2]
        bin_dir, dumps = _dump_tools(tmp_path)
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "https_proxy": "http://proxy.internal:3128",
            **install["env"],
            **GITHUB_CREDENTIALS,
        }
        _run_step(install["run"], env)
        _assert_scrubbed(dumps / "python.env", GITHUB_CREDENTIALS, keeps="https_proxy")


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
        assert len(job["script"]) == 3

    def test_github_workflow_shape(self) -> None:
        doc = yaml.safe_load(render_github_workflow(InstallTarget(pypi_version="0.2.0")))
        on = doc[True] if True in doc else doc["on"]
        assert "opened" in on["pull_request"]["types"]
        job = doc["jobs"]["review"]
        assert job["continue-on-error"] is True
        install, review = job["steps"][2], job["steps"][3]
        assert install["env"] == {"REPOACH_INSTALL": "repoach==0.2.0"}
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

    def test_default_pin_is_printed_and_never_a_branch(
        self, yoob: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = _init(yoob, monkeypatch, "--ci", "gitlab")
        pinned = next(line for line in out.splitlines() if line.startswith("init: pinned "))
        job = yaml.safe_load((yoob / GITLAB_JOB_PATH).read_text(encoding="utf-8"))
        install = job[GITLAB_JOB_NAME]["variables"]["REPOACH_INSTALL"]
        pin = pinned.removeprefix("init: pinned ").split(" (")[0]
        git_form = pin.replace(" @ ", " @ git+https://github.com/repoachhq/repoach@")
        assert install in (pin, git_form)
        assert not install.endswith(("@develop", "@main"))

    def test_unknown_ci_is_rc_2(self, yoob: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(yoob)
        result = CliRunner().invoke(app, ["init", "--ci", "jenkins"])
        assert result.exit_code == 2


def _commit(repo: Path) -> str:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


class TestResolvePin:
    def test_commit_pip_installed_from_git(self, tmp_path: Path) -> None:
        record = {
            "url": "https://github.com/repoachhq/repoach",
            "vcs_info": {"commit_id": "abc123"},
        }
        pin = resolve_pin(direct_url=record, source_dir=tmp_path, version="0.1.0")
        assert (pin.ref, pin.pypi_version) == ("abc123", None)

    def test_repoach_checkout_pins_its_head(self, tmp_path: Path) -> None:
        checkout = _git_repo(tmp_path / "repoach")
        package = checkout / "src" / "repoach"
        package.mkdir(parents=True)
        head = _commit(checkout)
        pin = resolve_pin(direct_url=None, source_dir=package, version="0.1.0")
        assert pin.ref == head
        assert "uncommitted" not in pin.provenance

    def test_wheel_inside_a_project_venv_never_pins_the_project_head(self, tmp_path: Path) -> None:
        project = _git_repo(tmp_path / "project")
        _commit(project)
        package = project / ".venv" / "lib" / "site-packages" / "repoach"
        package.mkdir(parents=True)
        pin = resolve_pin(direct_url=None, source_dir=package, version="0.3.1")
        assert (pin.ref, pin.pypi_version) == (None, "0.3.1")
        assert pin.requirement("https://x") == "repoach==0.3.1"

    def test_no_git_falls_back_to_the_pypi_version(self, tmp_path: Path) -> None:
        pin = resolve_pin(direct_url=None, source_dir=tmp_path, version="0.2.0")
        assert pin.describe() == "repoach==0.2.0 (the running version, from PyPI)"
