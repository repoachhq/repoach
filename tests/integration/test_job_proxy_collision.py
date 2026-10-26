"""SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB AC5/AC6: two jobs, two real proxies.

AC5 replays the overlap of runs 40301029793 and 40301669894 with the new
helper: two ``scripts/job_proxy.sh start`` run at the same time, get two
ports, and each ``/health`` echoes its own token; each job's token is
FOREIGN on the other job's port; ``stop`` leaves no process behind.

AC6 replays the OLD wiring on one fixed port: the second proxy fails to
bind (``Errno 98``), the old ``curl /health`` poll still succeeds because
the first proxy answers, and the new instance check names it foreign.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from repoach.llm_proxy.instance import EXIT_FOREIGN, EXIT_OWN, pick_free_port

_log = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[2]
_HELPER = _ROOT / "scripts" / "job_proxy.sh"


def _env(tmp_path: Path, name: str, token: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("GITHUB_ACTIONS", "REPOACH_PROXY_PORT", "REPOACH_PROXY_INSTANCE"):
        env.pop(key, None)
    env.update(
        {
            "PYTHON": sys.executable,
            "REPOACH_ANTHROPIC_AUTH_TOKEN": token,
            "JOB_PROXY_DIR": str(tmp_path / name),
            "JOB_PROXY_TIMEOUT_S": "60",
            "GITHUB_ENV": str(tmp_path / f"{name}.env"),
            "PYTHONPATH": os.pathsep.join(
                p for p in (str(_ROOT / "src"), env.get("PYTHONPATH", "")) if p
            ),
        }
    )
    return env


def _read_env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def _check(url: str, expect: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "repoach.llm_proxy.instance",
            "check",
            "--url",
            url,
            "--expect",
            expect,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError as exc:
        _log.debug("proxy pid %s is gone: %s", pid, exc)
        return False
    return True


def test_two_overlapping_jobs_get_two_ports_and_prove_their_own_proxy(tmp_path: Path) -> None:
    env_a = _env(tmp_path, "job_a", "token-a")
    env_b = _env(tmp_path, "job_b", "token-b")
    starts = [
        subprocess.Popen(
            ["bash", str(_HELPER), "start"],
            env=env,
            cwd=_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for env in (env_a, env_b)
    ]
    try:
        for proc in starts:
            _out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            assert "own proxy on 127.0.0.1:" in err
        a = _read_env(tmp_path / "job_a.env")
        b = _read_env(tmp_path / "job_b.env")
        assert a["REPOACH_PROXY_PORT"] != b["REPOACH_PROXY_PORT"]
        assert a["REPOACH_PROXY_INSTANCE"] != b["REPOACH_PROXY_INSTANCE"]
        assert a["REPOACH_LLM_PROXY_BASE_URL"] == f"http://127.0.0.1:{a['REPOACH_PROXY_PORT']}"

        for mine, other in ((a, b), (b, a)):
            url = f"{mine['REPOACH_LLM_PROXY_BASE_URL']}/health"
            own = _check(url, mine["REPOACH_PROXY_INSTANCE"], env_a)
            assert own.returncode == EXIT_OWN, own.stdout
            foreign = _check(url, other["REPOACH_PROXY_INSTANCE"], env_a)
            assert foreign.returncode == EXIT_FOREIGN
            assert foreign.stdout.startswith(
                f"foreign proxy on 127.0.0.1:{mine['REPOACH_PROXY_PORT']}"
            )
    finally:
        pids = [
            int((tmp_path / n / "llm_proxy.pid").read_text())
            for n in ("job_a", "job_b")
            if (tmp_path / n / "llm_proxy.pid").exists()
        ]
        for env in (env_a, env_b):
            subprocess.run(
                ["bash", str(_HELPER), "stop"], env=env, cwd=_ROOT, check=False, capture_output=True
            )
    for pid in pids:
        assert not _alive(pid), f"proxy pid {pid} survived stop"


def test_old_fixed_port_wiring_is_caught_as_foreign(tmp_path: Path) -> None:
    port = pick_free_port()
    url = f"http://127.0.0.1:{port}/health"
    first_env = _env(tmp_path, "first", "token-first")
    first_env.update({"REPOACH_PROXY_PORT": str(port), "REPOACH_PROXY_INSTANCE": "first-job"})
    second_env = _env(tmp_path, "second", "token-second")
    second_env.update({"REPOACH_PROXY_PORT": str(port), "REPOACH_PROXY_INSTANCE": "second-job"})
    second_log = tmp_path / "second.log"
    first = subprocess.Popen(
        [sys.executable, "-m", "repoach.llm_proxy"],
        env=first_env,
        cwd=_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    second = None
    try:
        deadline = time.monotonic() + 60
        while _check(url, "first-job", first_env).returncode != EXIT_OWN:
            assert time.monotonic() < deadline, "first proxy never became healthy"
            assert first.poll() is None, "first proxy exited"
            time.sleep(0.5)

        with second_log.open("w") as log:
            second = subprocess.Popen(
                [sys.executable, "-m", "repoach.llm_proxy"],
                env=second_env,
                cwd=_ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            second.wait(timeout=60)
        assert second.returncode != 0
        assert "[Errno 98]" in second_log.read_text()

        with urllib.request.urlopen(url, timeout=2) as resp:
            assert resp.status == 200

        verdict = _check(url, "second-job", second_env)
        assert verdict.returncode == EXIT_FOREIGN
        assert verdict.stdout.strip() == f"foreign proxy on 127.0.0.1:{port} (instance=first-job)"
    finally:
        for proc in (first, second):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()


@pytest.mark.parametrize("cmd", ["", "restart"])
def test_helper_rejects_unknown_commands(cmd: str) -> None:
    result = subprocess.run(
        ["bash", str(_HELPER), cmd], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr
