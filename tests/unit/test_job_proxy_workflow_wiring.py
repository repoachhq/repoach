"""SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB AC3/AC4: no job pins the proxy port.

Run 40301669894 failed because two review jobs on two runners of one host
both started their proxy on the hardcoded ``127.0.0.1:8083``. These tests
parse the committed workflows. Every job that starts a proxy materializes
the helper from a heredoc byte-identical to ``scripts/job_proxy.sh`` (the
job's checkout may predate the script), starts through it, names no port,
never sets the proxy keys in a step ``env:`` (that would shadow
``$GITHUB_ENV``), and stops its proxy in a last ``if: always()`` step.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.job_proxy_fakes import HELPER

_ROOT = Path(__file__).resolve().parents[2]
_AUTO_REVIEW = _ROOT / ".github" / "workflows" / "auto-review.yml"
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_PORT_LITERAL = re.compile(r"127\.0\.0\.1:\d+|localhost:\d+|PROXY_PORT")
_SHADOWING_KEYS = {"REPOACH_LLM_PROXY_BASE_URL", "REPOACH_PROXY_PORT", "REPOACH_PROXY_INSTANCE"}
_OPEN = "cat > \"$RUNNER_TEMP/job_proxy.sh\" <<'JOB_PROXY_SH'\n"
_CLOSE = "JOB_PROXY_SH\n"
_MATERIALIZE = "Materialize job proxy helper"
_PROXY_JOBS = ["review", "auto_fix"]


def _steps(path: Path, job: str) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"][job]["steps"]


def _named(steps: list[dict], name: str) -> int:
    return next(i for i, s in enumerate(steps) if s.get("name") == name)


def _step_text(step: dict) -> str:
    return yaml.safe_dump({"env": step.get("env", {}), "run": step.get("run", "")})


@pytest.mark.parametrize("job", _PROXY_JOBS)
def test_embedded_helper_is_byte_identical_to_the_script(job: str) -> None:
    run = _steps(_AUTO_REVIEW, job)[_named(_steps(_AUTO_REVIEW, job), _MATERIALIZE)]["run"]
    assert run.startswith(_OPEN) and run.endswith(_CLOSE)
    embedded = run[len(_OPEN) : -len(_CLOSE)]
    assert embedded == HELPER.read_text(encoding="utf-8"), (
        "auto-review.yml embeds a stale copy of scripts/job_proxy.sh; re-embed it"
    )


@pytest.mark.parametrize("job", _PROXY_JOBS)
def test_proxy_jobs_start_through_the_materialized_helper(job: str) -> None:
    steps = _steps(_AUTO_REVIEW, job)
    start = _named(steps, "Start LLM proxy sidecar")
    assert _named(steps, _MATERIALIZE) < start
    assert 'bash "$RUNNER_TEMP/job_proxy.sh" start' in steps[start]["run"]
    direct = re.compile(r"-m repoach\.llm_proxy(\s|$)")
    others = [s for i, s in enumerate(steps) if i != _named(steps, _MATERIALIZE)]
    assert not any(direct.search(s.get("run", "")) for s in others)


@pytest.mark.parametrize("job", _PROXY_JOBS)
def test_proxy_jobs_hardcode_no_port_and_do_not_shadow_github_env(job: str) -> None:
    for step in _steps(_AUTO_REVIEW, job):
        if step.get("name") == _MATERIALIZE:
            continue
        text = _step_text(step)
        assert not _PORT_LITERAL.search(text), f"{job}/{step.get('name')}: {text}"
        assert not _SHADOWING_KEYS & set(step.get("env", {})), step.get("name")


@pytest.mark.parametrize("job", _PROXY_JOBS)
def test_proxy_jobs_stop_their_proxy_last_and_always(job: str) -> None:
    last = _steps(_AUTO_REVIEW, job)[-1]
    assert last.get("if") == "always()"
    assert last.get("run", "").strip() == 'bash "$RUNNER_TEMP/job_proxy.sh" stop'


def test_ci_smoke_uses_the_helper_and_no_fixed_port() -> None:
    smoke = next(s for s in _steps(_CI, "test") if s.get("name") == "Smoke — llm_proxy boots")
    text = _step_text(smoke)
    assert "scripts/job_proxy.sh start" in text
    assert "scripts/job_proxy.sh stop" in text
    assert not _PORT_LITERAL.search(text)


def test_helper_exports_port_instance_and_url_and_imports_no_repoach_module() -> None:
    text = HELPER.read_text(encoding="utf-8")
    assert '>> "$GITHUB_ENV"' in text
    for key in ("REPOACH_PROXY_PORT=", "REPOACH_PROXY_INSTANCE=", "REPOACH_LLM_PROXY_BASE_URL="):
        assert key in text
    assert re.findall(r"repoach\.[a-z_.]+", text) == ["repoach.llm_proxy"]
    assert "/proc/net/tcp" in text and '/proc/"$pid"/fd' in text
