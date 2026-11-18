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


VERIFY_LINE = "repoach-llm-proxy.service: Command /nowhere/repoach is not executable"


def _stub_bin(tmp_path: Path, *, with_systemctl: bool, analyze: str = "real") -> Path:
    """Link the tools the script needs; ``analyze`` is ``real``, ``missing`` or ``failing``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "sed", "grep", "mktemp", "rm", "mkdir", "cp", "dirname", "git"):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    found = shutil.which("systemd-analyze")
    if analyze == "real" and found:
        (bin_dir / "systemd-analyze").symlink_to(found)
    elif analyze == "failing":
        stub = bin_dir / "systemd-analyze"
        stub.write_text(f'#!/bin/sh\necho "{VERIFY_LINE}" >&2\nexit 1\n', encoding="utf-8")
        stub.chmod(0o755)
    if with_systemctl:
        calls = tmp_path / "systemctl.calls"
        stub = bin_dir / "systemctl"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit 0\n', encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir


def _calls(tmp_path: Path) -> list[str]:
    calls = tmp_path / "systemctl.calls"
    return calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


def _run(args: list[str], bin_dir: Path) -> subprocess.CompletedProcess[str]:
    scratch = bin_dir.parent
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "HOME": str(scratch / "home"),
        "XDG_CONFIG_HOME": str(scratch / "xdg"),
    }
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
    assert _calls(tmp_path) == ["--user show-environment"]
    assert "daemon-reload: skipped (--dest is not the user unit dir)" in out


def test_only_the_user_unit_dir_is_reloaded(tmp_path: Path) -> None:
    """No --dest, or --dest naming the user unit dir, reloads; a scratch --dest does not."""
    home = _checkout(tmp_path)
    bin_dir = _stub_bin(tmp_path, with_systemctl=True, analyze="failing")
    (bin_dir / "systemd-analyze").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    user_dir = tmp_path / "xdg" / "systemd" / "user"

    default = _run(["--home", str(home)], bin_dir)
    assert default.returncode == 0, default.stderr
    assert sorted(p.name for p in user_dir.iterdir()) == sorted(SERVICES + TIMERS)
    assert _calls(tmp_path) == ["--user show-environment", "--user daemon-reload"]
    assert "daemon-reload: done" in default.stdout.splitlines()

    named = _run(["--home", str(home), "--dest", f"{user_dir}/"], bin_dir)
    assert named.returncode == 0, named.stderr
    assert _calls(tmp_path)[-1] == "--user daemon-reload"
    assert len(_calls(tmp_path)) == 4

    scratch = _run(["--home", str(home), "--dest", str(tmp_path / "scratch")], bin_dir)
    assert scratch.returncode == 0, scratch.stderr
    assert "daemon-reload: skipped (--dest is not the user unit dir)" in scratch.stdout.splitlines()
    assert _calls(tmp_path)[-1] == "--user show-environment"
    assert len(_calls(tmp_path)) == 5


def test_install_script_refuses_a_verification_that_prints(tmp_path: Path) -> None:
    """Any verify output is relayed and refuses: exit 2, nothing in --dest, no reload."""
    home = _checkout(tmp_path)
    dest = tmp_path / "dest"
    bin_dir = _stub_bin(tmp_path, with_systemctl=True, analyze="failing")
    done = _run(["--home", str(home), "--dest", str(dest)], bin_dir)
    assert done.returncode == 2
    assert VERIFY_LINE in done.stderr
    assert "systemd-analyze --user verify printed the lines above; nothing installed" in done.stderr
    assert not dest.exists()
    assert not (home / "logs").exists()
    assert "--user daemon-reload" not in _calls(tmp_path)


def test_install_script_refuses_without_systemd_analyze(tmp_path: Path) -> None:
    """No systemd-analyze on PATH is exit 2 with its reason, nothing in --dest, no reload."""
    home = _checkout(tmp_path)
    dest = tmp_path / "dest"
    bin_dir = _stub_bin(tmp_path, with_systemctl=True, analyze="missing")
    assert not (bin_dir / "systemd-analyze").exists()
    done = _run(["--home", str(home), "--dest", str(dest)], bin_dir)
    assert done.returncode == 2
    assert "systemd-analyze not found: the units cannot be verified" in done.stderr
    assert not dest.exists()
    assert "--user daemon-reload" not in _calls(tmp_path)


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
