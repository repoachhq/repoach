"""The systemd --user units are templates rendered with an absolute checkout path.

SP-SCRUB-PERSONAL-DATA-AT-HEAD D3: each tracked unit holds ``@REPOACH_HOME@``
where the checkout path stood, and ``scripts/install_user_units.sh`` renders,
verifies and installs them. The tests render into temporary directories with a
stub ``systemctl`` first on ``PATH``, so the invoking user's manager is never
reloaded.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UNITS_DIR = ROOT / "deploy" / "systemd"
SCRIPT = ROOT / "scripts" / "install_user_units.sh"
TOKEN = "@REPOACH_HOME@"
SERVICES = ("repoach-llm-proxy.service", "repoach-nim-health.service", "repoach-chainpilot.service")
TIMERS = ("repoach-nim-health.timer", "repoach-chainpilot.timer")


def _checkout(tmp_path: Path) -> Path:
    home = tmp_path / "checkout"
    (home / ".venv" / "bin").mkdir(parents=True)
    for name in ("repoach", "python"):
        stub = home / ".venv" / "bin" / name
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    return home


def _stub_bin(tmp_path: Path, *, with_systemctl: bool) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in (
        "bash",
        "sed",
        "grep",
        "mktemp",
        "rm",
        "mkdir",
        "cp",
        "dirname",
        "git",
        "systemd-analyze",
    ):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    if with_systemctl:
        calls = tmp_path / "systemctl.calls"
        stub = bin_dir / "systemctl"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit 0\n', encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir


def _run(args: list[str], bin_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": str(bin_dir)}
    return subprocess.run(
        [str(bin_dir / "bash"), str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_units_carry_the_token_and_no_layout() -> None:
    """Every service holds the token in WorkingDirectory= and ExecStart=; no layout under %h survives."""
    for name in SERVICES:
        lines = (UNITS_DIR / name).read_text(encoding="utf-8").splitlines()
        assert any(line.startswith(f"WorkingDirectory={TOKEN}") for line in lines), name
        assert any(line.startswith(f"ExecStart={TOKEN}/") for line in lines), name
    for path in [*(UNITS_DIR / n for n in SERVICES + TIMERS), UNITS_DIR / "README.md"]:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "%h/" in line:
                assert line.startswith("Environment=PATH="), f"{path.name}: {line}"


def test_rendered_units_verify_on_this_systemd(tmp_path: Path) -> None:
    """The script renders five files that systemd-analyze verifies with empty output."""
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze is not installed on this host")
    home = _checkout(tmp_path)
    dest = tmp_path / "dest"
    bin_dir = _stub_bin(tmp_path, with_systemctl=True)
    done = _run(["--home", str(home), "--dest", str(dest)], bin_dir)
    assert done.returncode == 0, done.stderr
    out = done.stdout.splitlines()
    assert sorted(line for line in out if line.startswith("installed: ")) == sorted(
        f"installed: {dest / n}" for n in SERVICES + TIMERS
    )
    assert sorted(line for line in out if line.startswith("verified: ")) == sorted(
        f"verified: {n}" for n in SERVICES
    )
    rendered = sorted(dest.iterdir())
    assert len(rendered) == 5
    for path in rendered:
        text = path.read_text(encoding="utf-8")
        assert TOKEN not in text
    for name in SERVICES:
        assert f"WorkingDirectory={home}\n" in (dest / name).read_text(encoding="utf-8")
    verify = subprocess.run(
        [analyze, "--user", "verify", *(str(p) for p in rendered)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert verify.stdout + verify.stderr == ""
    assert (home / "logs").is_dir()
    assert (tmp_path / "systemctl.calls").read_text(encoding="utf-8").splitlines() == [
        "--user show-environment",
        "--user daemon-reload",
    ]


def test_install_script_refuses_without_user_systemd_and_a_relative_home(tmp_path: Path) -> None:
    """No systemctl or a relative --home is exit 2 with its reason; --dry-run writes nothing."""
    home = _checkout(tmp_path)
    dest = tmp_path / "dest"
    bin_dir = _stub_bin(tmp_path, with_systemctl=False)

    no_systemctl = _run(["--home", str(home), "--dest", str(dest)], bin_dir)
    assert no_systemctl.returncode == 2
    assert "systemctl not found" in no_systemctl.stderr

    relative = _run(["--home", "x", "--dest", str(dest)], bin_dir)
    assert relative.returncode == 2
    assert "--home must be an absolute path" in relative.stderr

    dry = _run(["--home", str(home), "--dest", str(dest), "--dry-run"], bin_dir)
    assert dry.returncode == 0, dry.stderr
    headers = [line for line in dry.stdout.splitlines() if line.startswith("# --- ")]
    assert headers == [
        f"# --- {n}"
        for n in (
            "repoach-llm-proxy.service",
            "repoach-nim-health.service",
            "repoach-nim-health.timer",
            "repoach-chainpilot.service",
            "repoach-chainpilot.timer",
        )
    ]
    assert TOKEN not in dry.stdout
    assert f"WorkingDirectory={home}" in dry.stdout
    assert not dest.exists()
    assert not (home / "logs").exists()
