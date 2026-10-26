"""SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB AC1: ``job_proxy.sh prove``, PID proof then token.

Each case binds a fake ``/health`` listener whose PID the test owns and
runs one probe. The PID proof decides first: the listening socket's inode
(``/proc/net/tcp``) must be among the spawned PID's ``/proc/<pid>/fd``
links. The ``instance`` token is the second proof when the body has one.
"""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

from tests.job_proxy_fakes import HELPER, prove, start_fake_health, stop_process


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(
    tmp_path: Path, body: dict | str, status: int = 200
) -> tuple[subprocess.Popen[str], int]:
    port = _free_port()
    text = body if isinstance(body, str) else json.dumps(body)
    return start_fake_health(tmp_path, port, text, status), port


def test_own_pid_and_matching_token_is_own(tmp_path: Path) -> None:
    proc, port = _serve(tmp_path, {"status": "healthy", "instance": "job-a"})
    try:
        result = prove(proc.pid, port, "job-a")
    finally:
        stop_process(proc)
    assert result.returncode == 0
    assert (
        result.stdout.strip() == f"own proxy on 127.0.0.1:{port} (pid {proc.pid}, instance job-a)"
    )


def test_own_pid_without_echo_is_own_on_the_pid_proof_alone(tmp_path: Path) -> None:
    proc, port = _serve(tmp_path, {"status": "healthy"})
    try:
        result = prove(proc.pid, port, "job-a")
    finally:
        stop_process(proc)
    assert result.returncode == 0
    assert (
        "instance echo absent (proxy predates SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB)" in result.stdout
    )


def test_own_pid_with_another_token_is_foreign(tmp_path: Path) -> None:
    proc, port = _serve(tmp_path, {"status": "healthy", "instance": "job-b"})
    try:
        result = prove(proc.pid, port, "job-a")
    finally:
        stop_process(proc)
    assert result.returncode == 3
    assert result.stdout.strip() == f"foreign proxy on 127.0.0.1:{port} (instance=job-b)"


def test_listener_owned_by_another_pid_is_foreign_before_any_http(tmp_path: Path) -> None:
    proc, port = _serve(tmp_path, {"status": "healthy", "instance": "job-a"})
    bystander = subprocess.Popen(["sleep", "30"])
    try:
        result = prove(bystander.pid, port, "job-a")
    finally:
        stop_process(proc)
        stop_process(bystander)
    assert result.returncode == 3
    assert (
        result.stdout.strip()
        == f"foreign proxy on 127.0.0.1:{port} (listener not owned by pid {bystander.pid})"
    )


def test_dead_spawned_pid_fails_at_once(tmp_path: Path) -> None:
    dead = subprocess.Popen(["true"])
    dead.wait()
    result = prove(dead.pid, _free_port(), "job-a")
    assert result.returncode == 4
    assert result.stdout.startswith(f"spawned proxy (pid {dead.pid}) is dead")


def test_live_pid_without_listener_is_not_ready(tmp_path: Path) -> None:
    waiting = subprocess.Popen(["sleep", "30"])
    port = _free_port()
    try:
        result = prove(waiting.pid, port, "job-a")
    finally:
        stop_process(waiting)
    assert result.returncode == 1
    assert result.stdout.strip() == f"no listener on 127.0.0.1:{port} yet"


def test_own_listener_not_healthy_yet_is_not_ready(tmp_path: Path) -> None:
    proc, port = _serve(tmp_path, "starting", status=503)
    try:
        result = prove(proc.pid, port, "job-a")
    finally:
        stop_process(proc)
    assert result.returncode == 1
    assert "/health not healthy yet" in result.stdout


def test_prove_usage_and_unknown_commands_exit_2() -> None:
    for args in (["prove", "1"], [""], ["restart"]):
        result = subprocess.run(
            ["bash", str(HELPER), *args], capture_output=True, text=True, check=False
        )
        assert result.returncode == 2
        assert "usage:" in result.stderr
