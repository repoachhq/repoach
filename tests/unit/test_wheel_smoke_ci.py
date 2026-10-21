"""SP-PYPI-RELEASE G2: the PR wheel smoke in ``ci.yml`` and ``scripts/wheel_smoke.sh``."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_SCRIPT = _ROOT / "scripts" / "wheel_smoke.sh"


def _jobs() -> dict:
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))["jobs"]


def _step(job: dict, name: str) -> dict:
    matches = [s for s in job["steps"] if s.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def test_ci_builds_the_wheel_once_with_the_full_history() -> None:
    build = _jobs()["wheel-build"]
    assert build["steps"][0]["with"]["fetch-depth"] == 0
    run = _step(build, "Build sdist + wheel")["run"]
    assert "python -m build --sdist --wheel" in run
    assert "twine check dist/*" in run


def test_ci_smokes_the_built_wheel_on_both_pythons() -> None:
    job = _jobs()["wheel-smoke"]
    assert job["name"] == "wheel-smoke (${{ matrix.python-version }})"
    assert job["needs"] == "wheel-build"
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.13"]
    run = _step(job, "pipx install the wheel and run it outside the checkout")["run"]
    assert "scripts/wheel_smoke.sh" in run
    assert "dist/repoach-*.whl" in run


def test_ci_wheel_jobs_keep_the_untrusted_pr_gate() -> None:
    jobs = _jobs()
    assert jobs["wheel-build"]["if"] == jobs["test"]["if"]


def test_ci_actions_are_pinned_to_a_commit_sha() -> None:
    sha_pin = re.compile(r"^[^@]+@[0-9a-f]{40}(\s+#.*)?$")
    for line in _CI.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("uses:") or stripped.startswith("- uses:"):
            ref = stripped.split("uses:", 1)[1].strip()
            assert sha_pin.match(ref), f"action not pinned to a 40-hex SHA: {ref}"


def _smoke(*args: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run([bash, str(_SCRIPT), *args], capture_output=True, text=True, check=False)


def test_smoke_script_refuses_missing_arguments() -> None:
    done = _smoke()
    assert done.returncode == 2
    assert "usage:" in done.stderr


def test_smoke_script_refuses_a_missing_wheel(tmp_path: Path) -> None:
    done = _smoke("--python", "python3", str(tmp_path / "repoach-0.1.0-py3-none-any.whl"))
    assert done.returncode == 2
    assert "no such wheel" in done.stderr


def test_smoke_script_needs_an_expected_version_for_a_requirement() -> None:
    done = _smoke("--python", "python3", "repoach==0.1.0")
    assert done.returncode == 2
    assert "--expect is required" in done.stderr


def test_smoke_script_checks_the_four_commands_and_the_pin() -> None:
    text = _SCRIPT.read_text(encoding="utf-8")
    for needle in (
        '"$repoach" --version',
        '"$repoach" --help',
        '"$repoach" doctor',
        '"$repoach" init --ci gitlab --print',
        'REPOACH_INSTALL: \\"repoach==$expect\\"',
        "version mismatch: wheel",
    ):
        assert needle in text, needle
