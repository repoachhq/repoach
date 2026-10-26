"""SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB AC3/AC4: no job pins the proxy port.

Run 40301669894 failed because two review jobs on two runners of one host
both started their proxy on the hardcoded ``127.0.0.1:8083``. These tests
parse the committed workflows: every job that starts a proxy does it
through ``scripts/job_proxy.sh start`` (free port, instance token, health
proof, ``$GITHUB_ENV`` export), no step of those jobs names a port or sets
the proxy URL itself (a step ``env:`` would shadow ``$GITHUB_ENV``), and
the job stops its own proxy in an ``if: always()`` step.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_AUTO_REVIEW = _ROOT / ".github" / "workflows" / "auto-review.yml"
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_HELPER = _ROOT / "scripts" / "job_proxy.sh"
_PORT_LITERAL = re.compile(r"127\.0\.0\.1:\d+|localhost:\d+|PROXY_PORT")
_SHADOWING_KEYS = {"REPOACH_LLM_PROXY_BASE_URL", "REPOACH_PROXY_PORT", "REPOACH_PROXY_INSTANCE"}


def _steps(path: Path, job: str) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"][job]["steps"]


def _step_text(step: dict) -> str:
    return yaml.safe_dump({"env": step.get("env", {}), "run": step.get("run", "")})


@pytest.mark.parametrize("job", ["review", "auto_fix"])
def test_proxy_jobs_start_through_the_helper(job: str) -> None:
    steps = _steps(_AUTO_REVIEW, job)
    starts = [s for s in steps if "scripts/job_proxy.sh start" in s.get("run", "")]
    assert len(starts) == 1, f"{job}: expected one helper start step"
    direct = re.compile(r"-m repoach\.llm_proxy(\s|$)")
    assert not any(direct.search(s.get("run", "")) for s in steps)


@pytest.mark.parametrize("job", ["review", "auto_fix"])
def test_proxy_jobs_hardcode_no_port_and_do_not_shadow_github_env(job: str) -> None:
    for step in _steps(_AUTO_REVIEW, job):
        text = _step_text(step)
        assert not _PORT_LITERAL.search(text), f"{job}/{step.get('name')}: {text}"
        assert not _SHADOWING_KEYS & set(step.get("env", {})), step.get("name")


@pytest.mark.parametrize("job", ["review", "auto_fix"])
def test_proxy_jobs_stop_their_proxy_last_and_always(job: str) -> None:
    last = _steps(_AUTO_REVIEW, job)[-1]
    assert last.get("if") == "always()"
    assert last.get("run", "").strip() == "bash scripts/job_proxy.sh stop"


def test_ci_smoke_uses_the_helper_and_no_fixed_port() -> None:
    smoke = next(s for s in _steps(_CI, "test") if s.get("name") == "Smoke — llm_proxy boots")
    text = _step_text(smoke)
    assert "scripts/job_proxy.sh start" in text
    assert "scripts/job_proxy.sh stop" in text
    assert not _PORT_LITERAL.search(text)


def test_helper_exports_port_instance_and_url_to_github_env() -> None:
    text = _HELPER.read_text(encoding="utf-8")
    assert '>> "$GITHUB_ENV"' in text
    for key in ("REPOACH_PROXY_PORT=", "REPOACH_PROXY_INSTANCE=", "REPOACH_LLM_PROXY_BASE_URL="):
        assert key in text
    assert "repoach.llm_proxy.instance port" in text
    assert "repoach.llm_proxy.instance check" in text
