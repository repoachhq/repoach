"""Guard tests for the hand-maintained PyPI publish workflow and the PR wheel smoke.

`.github/workflows/publish.yml` is bot-forbidden and publishes the package to a
public index, so its trust-critical wiring must not silently regress: it fires
only on a version tag, uses OIDC trusted publishing (never a stored token) from
the ``pypi`` environment, publishes only after the wheel smoke and the version
check, and pins every third-party action to an immutable commit SHA
(SP-CI-SUPPLY-CHAIN-HARDEN G1, SP-PYPI-RELEASE G2/G3).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_PUBLISH = _WORKFLOWS / "publish.yml"
_RUNNER = "${{ vars.REPOACH_RUNNER || 'self-hosted' }}"


def _workflow(path: Path = _PUBLISH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _step(job: dict, name: str) -> dict:
    matches = [s for s in job["steps"] if s.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def test_publish_triggers_only_on_version_tags_and_the_dry_run() -> None:
    on_block = _workflow()[True]
    assert on_block["push"]["tags"] == ["v*"]
    assert "pull_request" not in on_block
    assert "workflow_dispatch" in on_block


def test_publish_job_uses_oidc_trusted_publishing_from_the_pypi_environment() -> None:
    job = _workflow()["jobs"]["build-and-publish"]
    assert job["permissions"]["id-token"] == "write"
    assert job["environment"]["name"] == "pypi"
    publish = [
        s for s in job["steps"] if str(s.get("uses", "")).startswith("pypa/gh-action-pypi-publish")
    ]
    assert len(publish) == 1
    assert "with" not in publish[0] or "password" not in publish[0]["with"]


def test_publish_needs_the_build_and_the_smoke_and_only_runs_on_a_tag_push() -> None:
    job = _workflow()["jobs"]["build-and-publish"]
    assert set(job["needs"]) == {"build", "wheel-smoke"}
    assert job["if"] == "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"


def test_only_the_publish_job_holds_an_oidc_token() -> None:
    jobs = _workflow()["jobs"]
    holders = [n for n, j in jobs.items() if j.get("permissions", {}).get("id-token") == "write"]
    assert holders == ["build-and-publish"]


def test_release_and_pypi_smoke_follow_the_upload() -> None:
    jobs = _workflow()["jobs"]
    assert "build-and-publish" in jobs["github-release"]["needs"]
    assert jobs["github-release"]["permissions"]["contents"] == "write"
    assert "build-and-publish" in jobs["pypi-smoke"]["needs"]
    smoke = _step(jobs["pypi-smoke"], "pipx install the release from PyPI and run it")
    assert "--index-url https://pypi.org/simple" in smoke["run"]
    wait = _step(jobs["pypi-smoke"], "Wait for the index to serve the release (at most 10 minutes)")
    assert "seq 1 20" in wait["run"]


def test_the_build_stops_when_the_tag_is_not_on_main() -> None:
    build = _workflow()["jobs"]["build"]
    step = _step(build, "The tag is on main")
    assert "git merge-base --is-ancestor" in step["run"]
    names = [s.get("name") for s in build["steps"]]
    assert names.index("The tag is on main") < names.index("Build sdist + wheel")


def test_every_job_runs_on_the_configurable_runner() -> None:
    for name, job in _workflow()["jobs"].items():
        assert job["runs-on"] == _RUNNER, name


@pytest.mark.parametrize("path", [_PUBLISH], ids=["publish"])
def test_every_action_is_pinned_to_a_commit_sha(path: Path) -> None:
    sha_pin = re.compile(r"^[^@]+@[0-9a-f]{40}(\s+#.*)?$")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("uses:") or stripped.startswith("- uses:"):
            ref = stripped.split("uses:", 1)[1].strip()
            assert sha_pin.match(ref), f"action not pinned to a 40-hex SHA: {ref}"


@pytest.mark.parametrize("path", [_PUBLISH], ids=["publish"])
def test_wheel_smoke_runs_on_both_pythons_through_the_shared_script(path: Path) -> None:
    job = _workflow(path)["jobs"]["wheel-smoke"]
    assert job["name"] == "wheel-smoke (${{ matrix.python-version }})"
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.13"]
    run = _step(job, "pipx install the wheel and run it outside the checkout")["run"]
    assert "scripts/wheel_smoke.sh" in run
    assert "dist/repoach-*.whl" in run


def _version_check(
    tmp_path: Path, wheel: str, event: str, ref: str
) -> subprocess.CompletedProcess[str]:
    script = _step(_workflow()["jobs"]["build"], "Version matches the tag")["run"]
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / wheel).write_bytes(b"")
    output = tmp_path / "out"
    output.write_text("")
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, "-e", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "GITHUB_EVENT_NAME": event,
            "GITHUB_REF_NAME": ref,
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_version_check_refuses_a_wheel_that_is_not_the_tag(tmp_path: Path) -> None:
    done = _version_check(tmp_path, "repoach-0.1.0-py3-none-any.whl", "push", "v0.0.0-mismatch")
    assert done.returncode == 1
    assert "version mismatch: wheel 0.1.0, tag 0.0.0" in done.stdout


def test_version_check_passes_the_tag_version_on(tmp_path: Path) -> None:
    done = _version_check(tmp_path, "repoach-0.1.0-py3-none-any.whl", "push", "v0.1.0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "out").read_text().splitlines() == ["version=0.1.0", "prerelease=false"]


def test_version_check_marks_a_release_candidate_as_prerelease(tmp_path: Path) -> None:
    done = _version_check(tmp_path, "repoach-0.1.0rc1-py3-none-any.whl", "push", "v0.1.0rc1")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "prerelease=true" in (tmp_path / "out").read_text()


def test_dry_run_reports_the_wheel_version_without_a_tag(tmp_path: Path) -> None:
    wheel = "repoach-0.1.0.dev3+gabc1234-py3-none-any.whl"
    done = _version_check(tmp_path, wheel, "workflow_dispatch", "develop")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "dry run: wheel 0.1.0.dev3+gabc1234, no tag" in done.stdout
