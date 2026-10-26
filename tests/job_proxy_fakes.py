"""Fakes for SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB: a tiny /health listener and helper runners.

``FAKE_HEALTH_SERVER`` is a standard-library HTTP server that binds a port
and answers ``GET /health`` with a fixed body, standing in for a proxy whose
PID the test controls. ``write_base_proxy_package`` writes a shadow
``repoach.llm_proxy`` package whose ``__main__`` behaves like a proxy that
predates the ``instance`` echo: it binds ``REPOACH_PROXY_PORT`` and answers
``{"status": "healthy"}`` only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "job_proxy.sh"

FAKE_HEALTH_SERVER = """
import http.server
import os
import sys

port = int(sys.argv[1])
body = sys.argv[2].encode()
status = int(sys.argv[3]) if len(sys.argv) > 3 else 200


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


server = http.server.HTTPServer(("127.0.0.1", port), Handler)
print("ready", flush=True)
server.serve_forever()
"""

BASE_PROXY_MAIN = """
import http.server
import json
import os

port = int(os.environ["REPOACH_PROXY_PORT"])
host = os.environ.get("REPOACH_PROXY_HOST", "127.0.0.1")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "healthy"}).encode())

    def log_message(self, *args):
        return


http.server.HTTPServer((host, port), Handler).serve_forever()
"""


def start_fake_health(
    tmp_path: Path, port: int, body: str, status: int = 200
) -> subprocess.Popen[str]:
    """Start the fake listener on ``port`` and return once it is bound."""
    script = tmp_path / "fake_health.py"
    script.write_text(FAKE_HEALTH_SERVER)
    proc = subprocess.Popen(
        [sys.executable, str(script), str(port), body, str(status)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    return proc


def write_base_proxy_package(tmp_path: Path) -> Path:
    """Write a shadow ``repoach.llm_proxy`` without the instance echo; return its root."""
    pkg = tmp_path / "base_src" / "repoach" / "llm_proxy"
    pkg.mkdir(parents=True)
    (pkg.parent / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(BASE_PROXY_MAIN)
    return pkg.parent.parent


def helper_env(
    tmp_path: Path, name: str, token: str, pythonpath: str | None = None
) -> dict[str, str]:
    """Environment for one helper run, isolated under ``tmp_path / name``."""
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
            "PYTHONPATH": pythonpath
            or os.pathsep.join(p for p in (str(ROOT / "src"), env.get("PYTHONPATH", "")) if p),
        }
    )
    return env


def prove(pid: int, port: int, instance: str) -> subprocess.CompletedProcess[str]:
    """Run one ``job_proxy.sh prove`` probe."""
    return subprocess.run(
        ["bash", str(HELPER), "prove", str(pid), str(port), instance],
        env={**os.environ, "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )


def read_env_file(path: Path) -> dict[str, str]:
    """Parse the KEY=VALUE lines the helper appended to a ``$GITHUB_ENV`` file."""
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def stop_process(proc: subprocess.Popen[str] | None) -> None:
    """Terminate ``proc`` if it is still running."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
