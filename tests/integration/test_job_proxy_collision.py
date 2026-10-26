"""SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB AC5/AC6/AC7: real proxies, real collisions.

AC5 replays the overlap of runs 40301029793 and 40301669894 with the new
helper: two concurrent ``start`` calls get two ports, each passes the PID
and token proofs for itself, each fails both proofs for the other job.

AC6 replays the OLD wiring on one fixed port: the second proxy fails to
bind (``Errno 98``), the old ``GET /health`` poll still gets 200 from the
first proxy, and ``prove`` refuses it — dead spawned PID, or a listener
not owned by the prober's PID.

AC7 is this PR's own review job: a base-ref proxy that predates the
``instance`` echo still starts, on the PID proof alone.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from tests.job_proxy_fakes import (
    HELPER,
    ROOT,
    helper_env,
    prove,
    read_env_file,
    stop_process,
    write_base_proxy_package,
)


def _start(env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["bash", str(HELPER), "start"],
        env=env,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop(env: dict[str, str]) -> None:
    subprocess.run(
        ["bash", str(HELPER), "stop"], env=env, cwd=ROOT, check=False, capture_output=True
    )


def _pid(tmp_path: Path, name: str) -> int:
    return int((tmp_path / name / "llm_proxy.pid").read_text())


def _gone(pid: int) -> bool:
    return (
        not Path(f"/proc/{pid}").exists()
        or "zombie" in Path(f"/proc/{pid}/status").read_text().lower()
    )


def test_two_overlapping_jobs_get_two_ports_and_prove_their_own_proxy(tmp_path: Path) -> None:
    env_a = helper_env(tmp_path, "job_a", "token-a")
    env_b = helper_env(tmp_path, "job_b", "token-b")
    starts = [_start(env) for env in (env_a, env_b)]
    try:
        for proc in starts:
            _out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            assert "own proxy on 127.0.0.1:" in err
        a = read_env_file(tmp_path / "job_a.env")
        b = read_env_file(tmp_path / "job_b.env")
        a["pid"], b["pid"] = str(_pid(tmp_path, "job_a")), str(_pid(tmp_path, "job_b"))
        assert a["REPOACH_PROXY_PORT"] != b["REPOACH_PROXY_PORT"]
        assert a["REPOACH_LLM_PROXY_BASE_URL"] == f"http://127.0.0.1:{a['REPOACH_PROXY_PORT']}"

        for mine, other in ((a, b), (b, a)):
            port = int(mine["REPOACH_PROXY_PORT"])
            own = prove(int(mine["pid"]), port, mine["REPOACH_PROXY_INSTANCE"])
            assert own.returncode == 0, own.stdout
            assert f"instance {mine['REPOACH_PROXY_INSTANCE']}" in own.stdout
            wrong_pid = prove(int(other["pid"]), port, mine["REPOACH_PROXY_INSTANCE"])
            assert wrong_pid.returncode == 3
            assert f"listener not owned by pid {other['pid']}" in wrong_pid.stdout
            wrong_token = prove(int(mine["pid"]), port, other["REPOACH_PROXY_INSTANCE"])
            assert wrong_token.returncode == 3
            assert f"(instance={mine['REPOACH_PROXY_INSTANCE']})" in wrong_token.stdout
    finally:
        pids = [
            _pid(tmp_path, n)
            for n in ("job_a", "job_b")
            if (tmp_path / n / "llm_proxy.pid").exists()
        ]
        for env in (env_a, env_b):
            _stop(env)
    time.sleep(0.5)
    for pid in pids:
        assert _gone(pid), f"proxy pid {pid} survived stop"


def test_old_fixed_port_wiring_is_refused_by_the_pid_proof(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/health"
    first_env = helper_env(tmp_path, "first", "token-first")
    first_env.update({"REPOACH_PROXY_PORT": str(port), "REPOACH_PROXY_INSTANCE": "first-job"})
    second_env = helper_env(tmp_path, "second", "token-second")
    second_env.update({"REPOACH_PROXY_PORT": str(port), "REPOACH_PROXY_INSTANCE": "second-job"})
    second_log = tmp_path / "second.log"
    first = subprocess.Popen(
        [sys.executable, "-m", "repoach.llm_proxy"],
        env=first_env,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    second = None
    try:
        deadline = time.monotonic() + 60
        while prove(first.pid, port, "first-job").returncode != 0:
            assert time.monotonic() < deadline, "first proxy never became healthy"
            assert first.poll() is None, "first proxy exited"
            time.sleep(0.5)

        with second_log.open("w") as log:
            second = subprocess.Popen(
                [sys.executable, "-m", "repoach.llm_proxy"],
                env=second_env,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            second.wait(timeout=60)
        assert second.returncode != 0
        assert "[Errno 98]" in second_log.read_text()

        with urllib.request.urlopen(url, timeout=2) as resp:
            assert resp.status == 200

        dead = prove(second.pid, port, "second-job")
        assert dead.returncode == 4
        assert dead.stdout.startswith(f"spawned proxy (pid {second.pid}) is dead")

        racing = prove(os.getpid(), port, "second-job")
        assert racing.returncode == 3
        assert (
            racing.stdout.strip()
            == f"foreign proxy on 127.0.0.1:{port} (listener not owned by pid {os.getpid()})"
        )
    finally:
        stop_process(first)
        stop_process(second)


def test_base_proxy_without_instance_echo_starts_on_the_pid_proof(tmp_path: Path) -> None:
    base_src = write_base_proxy_package(tmp_path)
    env = helper_env(tmp_path, "base", "token-base", pythonpath=str(base_src))
    proc = _start(env)
    try:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        assert "instance echo absent (proxy predates SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB)" in err
        exported = read_env_file(tmp_path / "base.env")
        assert out.splitlines() == [f"{k}={exported[k]}" for k in exported]
        with urllib.request.urlopen(
            f"{exported['REPOACH_LLM_PROXY_BASE_URL']}/health", timeout=2
        ) as resp:
            assert resp.read() == b'{"status": "healthy"}'
    finally:
        pid = _pid(tmp_path, "base")
        _stop(env)
    time.sleep(0.5)
    assert _gone(pid)
