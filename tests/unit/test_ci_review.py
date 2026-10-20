"""SP-UNIVERSAL-CI-ENTRY: ``repoach ci review`` on GitLab CI and GitHub Actions.

Boundary fakes only: a fake forge state reader, a fake ``repoach review
pr`` runner returning ``(rc, stdout)``, a fake gateway starter, and, for
the two process seams, a fake interpreter script. The advisory contract
pinned here is Yoob's ``ci/review-advisory.sh`` one: each missing input
is a named skip; posting only on ``REPOACH_ADVISORY_POST=1``; exit 2 is
split by the JSON verdict; the provider key never reaches the review
and the forge token never reaches the gateway.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoach.ci_entry.advisory import (
    EXIT_CODE_TABLE,
    CiReviewPlan,
    CiReviewReport,
    Outcome,
    memory_reachable,
    plan_ci_review,
    run_ci_review,
    subprocess_review_runner,
)
from repoach.ci_entry.gateway import (
    GatewayHandle,
    GatewayStartError,
    gateway_env,
    loopback_gateway,
)
from repoach.ci_entry.variables import PASSTHROUGH_VARS
from repoach.cli.main import app
from repoach.forge import ForgeError

GITLAB_ENV: dict[str, str] = {
    "GITLAB_CI": "true",
    "CI_MERGE_REQUEST_IID": "283",
    "CI_PROJECT_ID": "42",
    "CI_SERVER_URL": "https://gitlab.example.com",
    "REPOACH_GITLAB_TOKEN": "glpat-secret",
}

GATEWAY_VARS: dict[str, str] = {
    "REPOACH_LLM_PROXY_BASE_URL": "http://gateway.internal:8084",
    "REPOACH_ANTHROPIC_AUTH_TOKEN": "gateway-secret",
}

DIRECT_VARS: dict[str, str] = {"REPOACH_ANTHROPIC_API_KEY": "sk-ant-secret"}


GITLAB_CREDENTIALS: dict[str, str] = {
    "CI_REGISTRY_PASSWORD": "registry-secret",
    "CI_DEPENDENCY_PROXY_PASSWORD": "proxy-secret",
    "CI_JOB_JWT": "jwt-secret",
    "CI_JOB_JWT_V2": "jwt2-secret",
    "CI_JOB_TOKEN": "job-secret",
    "GITLAB_TOKEN": "glpat-secret",
    "REPOACH_GITLAB_TOKEN": "glpat-secret",
    "REPOACH_ANTHROPIC_API_KEY": "sk-ant-secret",
    "PROJECT_MASKED_DB_PASSWORD": "db-secret",
}
"""A GitLab job's credentials: the known names and one nobody can predict."""

GITHUB_CREDENTIALS: dict[str, str] = {
    "ACTIONS_RUNTIME_TOKEN": "runtime-secret",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-secret",
    "GITHUB_TOKEN": "ghs-secret",
    "GH_TOKEN": "ghs-secret",
    "REPOACH_ANTHROPIC_API_KEY": "sk-ant-secret",
    "ORG_DEPLOY_KEY": "deploy-secret",
}


def _interpreter_env() -> dict[str, str]:
    """The parent's interpreter variables (PATH, LD_LIBRARY_PATH…), as a CI runner sets them.

    A child built from a hand-written ``{"PATH": …}`` loses
    ``LD_LIBRARY_PATH``, and an ``actions/setup-python`` interpreter then
    exits 127 before running a line (PR #217 CI).
    """
    return {name: os.environ[name] for name in PASSTHROUGH_VARS if name in os.environ}


def _github_env(tmp_path: Path) -> dict[str, str]:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 17}}), encoding="utf-8")
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_REPOSITORY": "acme/widget",
        "GITHUB_TOKEN": "ghs-secret",
    }


def _review_stdout(verdict: str, posted: int = 0) -> str:
    return json.dumps(
        {"pr_number": 1, "final_verdict": verdict, "posted_comments": posted, "posted_reviews": 0}
    )


class FakeRun:
    """Records each fake ``review pr`` call and the gateway it was handed."""

    def __init__(self, rc: int = 0, stdout: str | None = None, state: str = "open") -> None:
        self.rc = rc
        self.stdout = _review_stdout("APPROVE") if stdout is None else stdout
        self.state = state
        self.review_envs: list[dict[str, str]] = []
        self.gateway_envs: list[Mapping[str, str]] = []
        self.state_reads: list[int] = []

    def runner(self, plan: CiReviewPlan, env: dict[str, str]) -> tuple[int, str]:
        self.review_envs.append(env)
        return self.rc, self.stdout

    def pr_state(self, plan: CiReviewPlan) -> str:
        self.state_reads.append(plan.pr_number)
        return self.state

    @contextmanager
    def gateway(self, env: Mapping[str, str]) -> Iterator[GatewayHandle]:
        self.gateway_envs.append(env)
        yield GatewayHandle(base_url="http://127.0.0.1:9999", auth_token="per-job")


def _run(env: dict[str, str], fake: FakeRun, **flags: bool) -> CiReviewReport:
    planned = plan_ci_review(env, **flags)
    if isinstance(planned, CiReviewReport):
        return planned
    return run_ci_review(
        planned,
        env,
        pr_state=fake.pr_state,
        review_runner=fake.runner,
        gateway_starter=fake.gateway,
        bot_username="repoach-bot",
        memory_probe=lambda url: False,
    )


class TestNamedSkips:
    def test_no_pull_request(self) -> None:
        fake = FakeRun()
        report = _run({"GITLAB_CI": "true", **GATEWAY_VARS}, fake)
        assert report.outcome == Outcome.SKIPPED
        assert "CI_MERGE_REQUEST_IID" in report.reason
        assert report.exit_code == 0
        assert fake.review_envs == [] and fake.state_reads == []

    def test_no_gitlab_token(self) -> None:
        env = {k: v for k, v in GITLAB_ENV.items() if k != "REPOACH_GITLAB_TOKEN"}
        report = _run({**env, **GATEWAY_VARS}, FakeRun())
        assert report.outcome == Outcome.SKIPPED
        assert report.reason == "skipped, GITLAB_TOKEN / REPOACH_GITLAB_TOKEN is not set"

    def test_no_github_token(self, tmp_path: Path) -> None:
        env = {k: v for k, v in _github_env(tmp_path).items() if k != "GITHUB_TOKEN"}
        report = _run({**env, **DIRECT_VARS}, FakeRun())
        assert report.reason == "skipped, GH_TOKEN / GITHUB_TOKEN is not set"

    def test_no_provider(self) -> None:
        report = _run(dict(GITLAB_ENV), FakeRun())
        assert report.outcome == Outcome.SKIPPED
        assert "REPOACH_ANTHROPIC_API_KEY" in report.reason
        assert "REPOACH_LLM_PROXY_BASE_URL" in report.reason

    def test_gateway_url_without_its_secret(self) -> None:
        env = {**GITLAB_ENV, "REPOACH_LLM_PROXY_BASE_URL": "http://gw"}
        report = _run(env, FakeRun())
        assert "REPOACH_ANTHROPIC_AUTH_TOKEN is not set" in report.reason

    def test_strict_turns_a_skip_into_rc_2(self) -> None:
        report = _run(dict(GITLAB_ENV), FakeRun(), strict=True)
        assert report.outcome == Outcome.SKIPPED
        assert report.exit_code == 2


class TestGitLab:
    def test_gateway_mode_dry_run_by_default(self) -> None:
        fake = FakeRun()
        report = _run({**GITLAB_ENV, **GATEWAY_VARS}, fake)
        assert report.outcome == Outcome.REVIEWED
        assert (report.forge, report.forge_source) == ("gitlab", "GITLAB_CI")
        assert (report.pr_number, report.pr_source) == (283, "CI_MERGE_REQUEST_IID")
        assert (report.mode, report.provider_mode) == ("dry-run", "gateway")
        assert report.final_verdict == "APPROVE"
        assert fake.gateway_envs == []
        env = fake.review_envs[0]
        assert env["GITLAB_TOKEN"] == "glpat-secret"
        assert env["GITLAB_HOST"] == "gitlab.example.com"
        assert env["REPOACH_FORGE"] == "gitlab"
        assert env["REPOACH_LLM_PROXY_BASE_URL"] == "http://gateway.internal:8084"

    def test_post_only_on_exactly_one(self) -> None:
        posting = _run({**GITLAB_ENV, **GATEWAY_VARS, "REPOACH_ADVISORY_POST": "1"}, FakeRun())
        not_posting = _run(
            {**GITLAB_ENV, **GATEWAY_VARS, "REPOACH_ADVISORY_POST": "true"}, FakeRun()
        )
        assert posting.mode == "post"
        assert not_posting.mode == "dry-run"

    def test_direct_mode_keeps_the_key_out_of_the_review(self) -> None:
        fake = FakeRun(stdout=_review_stdout("COMMENT", posted=2))
        report = _run({**GITLAB_ENV, **DIRECT_VARS, "REPOACH_ADVISORY_POST": "1"}, fake)
        assert (report.outcome, report.provider_mode) == (Outcome.REVIEWED, "direct")
        assert report.posted_comments == 2
        env = fake.review_envs[0]
        assert "REPOACH_ANTHROPIC_API_KEY" not in env
        assert env["REPOACH_LLM_PROXY_BASE_URL"] == "http://127.0.0.1:9999"
        assert env["REPOACH_ANTHROPIC_AUTH_TOKEN"] == "per-job"
        assert fake.gateway_envs[0]["REPOACH_ANTHROPIC_API_KEY"] == "sk-ant-secret"

    def test_already_merged_reviews_nothing(self) -> None:
        fake = FakeRun(state="merged")
        report = _run({**GITLAB_ENV, **GATEWAY_VARS}, fake, strict=True)
        assert report.outcome == Outcome.ALREADY_MERGED
        assert report.exit_code == 0
        assert fake.review_envs == []

    def test_closed_reviews_nothing(self) -> None:
        fake = FakeRun(state="closed")
        assert _run({**GITLAB_ENV, **GATEWAY_VARS}, fake).outcome == Outcome.CLOSED
        assert fake.review_envs == []

    def test_unreadable_state_is_unavailable(self) -> None:
        fake = FakeRun()

        def broken(plan: CiReviewPlan) -> str:
            raise ForgeError("glab: 401 Unauthorized")

        planned = plan_ci_review({**GITLAB_ENV, **GATEWAY_VARS}, strict=True)
        assert isinstance(planned, CiReviewPlan)
        report = run_ci_review(
            planned,
            {**GITLAB_ENV, **GATEWAY_VARS},
            pr_state=broken,
            review_runner=fake.runner,
            gateway_starter=fake.gateway,
            bot_username="repoach-bot",
        )
        assert report.outcome == Outcome.UNAVAILABLE
        assert "401" in report.reason
        assert report.exit_code == 2
        assert fake.review_envs == []


class TestGitHub:
    def test_gateway_mode(self, tmp_path: Path) -> None:
        fake = FakeRun()
        report = _run({**_github_env(tmp_path), **GATEWAY_VARS}, fake)
        assert (report.forge, report.pr_number, report.pr_source) == (
            "github",
            17,
            "GITHUB_EVENT_PATH",
        )
        assert report.provider_mode == "gateway"
        assert fake.review_envs[0]["GH_REPO"] == "acme/widget"
        assert "GITLAB_TOKEN" not in fake.review_envs[0]

    def test_direct_mode_gateway_gets_no_forge_token(self, tmp_path: Path) -> None:
        fake = FakeRun()
        report = _run({**_github_env(tmp_path), **DIRECT_VARS}, fake)
        assert report.provider_mode == "direct"
        assert fake.review_envs[0]["GITHUB_TOKEN"] == "ghs-secret"
        assert "REPOACH_ANTHROPIC_API_KEY" not in fake.review_envs[0]

    def test_pr_flag_and_forge_flag_outside_ci(self) -> None:
        env = {"GH_TOKEN": "t", **GATEWAY_VARS}
        report = _run(env, FakeRun(), pr_override=5, forge_override="github")
        assert (report.forge_source, report.pr_source) == ("flag", "flag")


class TestInterpretation:
    def _gitlab(self, rc: int, stdout: str, strict: bool = False) -> CiReviewReport:
        return _run({**GITLAB_ENV, **GATEWAY_VARS}, FakeRun(rc=rc, stdout=stdout), strict=strict)

    def test_request_changes_is_advice_not_an_outage(self) -> None:
        report = self._gitlab(2, _review_stdout("REQUEST_CHANGES"))
        assert report.outcome == Outcome.REVIEWED
        assert report.final_verdict == "REQUEST_CHANGES"
        assert report.exit_code == 0
        assert self._gitlab(2, _review_stdout("REQUEST_CHANGES"), strict=True).exit_code == 4

    def test_exit_2_without_a_verdict_is_an_outage(self) -> None:
        report = self._gitlab(2, "")
        assert report.outcome == Outcome.UNAVAILABLE
        assert report.exit_code == 0
        assert self._gitlab(2, "", strict=True).exit_code == 2

    def test_reviewer_error(self) -> None:
        report = self._gitlab(3, _review_stdout("ERROR"), strict=True)
        assert report.outcome == Outcome.REVIEWER_ERROR
        assert report.exit_code == 3
        assert self._gitlab(3, _review_stdout("ERROR")).exit_code == 0

    def test_other_exit_code_is_internal(self) -> None:
        report = self._gitlab(1, "Traceback", strict=True)
        assert report.outcome == Outcome.INTERNAL_ERROR
        assert report.exit_code == 1

    def test_review_payload_is_carried_verbatim(self) -> None:
        report = self._gitlab(0, _review_stdout("APPROVE"))
        assert report.review is not None and report.review["final_verdict"] == "APPROVE"

    def test_exit_code_table_has_five_rows(self) -> None:
        assert [code for code, _ in EXIT_CODE_TABLE] == [0, 1, 2, 3, 4]

    def test_gateway_that_never_starts_is_unavailable(self) -> None:
        fake = FakeRun()

        @contextmanager
        def failing(env: Mapping[str, str]) -> Iterator[GatewayHandle]:
            raise GatewayStartError("exited with 1")
            yield GatewayHandle(base_url="", auth_token="")

        planned = plan_ci_review({**GITLAB_ENV, **DIRECT_VARS})
        assert isinstance(planned, CiReviewPlan)
        report = run_ci_review(
            planned,
            GITLAB_ENV | DIRECT_VARS,
            pr_state=fake.pr_state,
            review_runner=fake.runner,
            gateway_starter=failing,
            bot_username="repoach-bot",
        )
        assert report.outcome == Outcome.UNAVAILABLE
        assert "did not start" in report.reason
        assert fake.review_envs == []


def _fake_python(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fakepython"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


class TestProcessSeams:
    def test_subprocess_runner_passes_argv_env_and_captures_stdout(self, tmp_path: Path) -> None:
        python = _fake_python(
            tmp_path,
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'forge': os.environ.get('REPOACH_FORGE')}))\n"
            "sys.exit(2)\n",
        )
        plan = CiReviewPlan(
            forge="gitlab",
            forge_source="GITLAB_CI",
            pr_number=283,
            pr_source="CI_MERGE_REQUEST_IID",
            mode="dry-run",
            provider_mode="gateway",
            forge_token_var="GITLAB_TOKEN",
        )
        rc, stdout = subprocess_review_runner(tmp_path, python)(
            plan, {**_interpreter_env(), "REPOACH_FORGE": "gitlab"}
        )
        assert rc == 2
        payload = json.loads(stdout)
        assert payload["argv"] == [
            "-m",
            "repoach",
            "review",
            "pr",
            "283",
            "--forge",
            "gitlab",
            "--dry-run",
        ]
        assert payload["forge"] == "gitlab"

    @pytest.mark.parametrize("credentials", [GITLAB_CREDENTIALS, GITHUB_CREDENTIALS])
    def test_loopback_gateway_env_and_health(
        self, tmp_path: Path, credentials: dict[str, str]
    ) -> None:
        record = tmp_path / "gateway-env.json"
        python = _fake_python(
            tmp_path,
            "import http.server, json, os\n"
            f"open({str(record)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200)\n"
            "        self.end_headers()\n"
            "    def log_message(self, *args):\n"
            "        pass\n"
            "port = int(os.environ['REPOACH_PROXY_PORT'])\n"
            "http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()\n",
        )
        env = {
            **_interpreter_env(),
            "HTTPS_PROXY": "http://proxy.internal:3128",
            "REPOACH_MODEL_SONNET": "claude-sonnet-x",
            **credentials,
        }
        with loopback_gateway(env, python=python, timeout_s=20, poll_s=0.1) as gateway:
            assert gateway.base_url.startswith("http://127.0.0.1:")
            seen = json.loads(record.read_text(encoding="utf-8"))
        assert seen["REPOACH_ANTHROPIC_API_KEY"] == "sk-ant-secret"
        assert seen["REPOACH_ANTHROPIC_AUTH_TOKEN"] == gateway.auth_token
        assert seen["REPOACH_PROXY_HOST"] == "127.0.0.1"
        assert seen["MODEL_SONNET"] == "anthropic/claude-sonnet-x"
        assert seen["MODEL_OPUS"] == "anthropic/claude-opus-5"
        assert seen["HTTPS_PROXY"] == "http://proxy.internal:3128"
        leaked = {k for k, v in seen.items() if v in credentials.values()}
        assert leaked == {"REPOACH_ANTHROPIC_API_KEY"}
        assert set(seen).isdisjoint(set(credentials) - {"REPOACH_ANTHROPIC_API_KEY"})

    def test_loopback_gateway_that_exits_names_its_log(self, tmp_path: Path) -> None:
        python = _fake_python(tmp_path, "import sys\nprint('boom: no upstream')\nsys.exit(1)\n")
        env = {**_interpreter_env(), "REPOACH_ANTHROPIC_API_KEY": "k"}
        gateway = loopback_gateway(env, python=python, timeout_s=20, poll_s=0.1)
        with pytest.raises(GatewayStartError) as info, gateway:
            raise AssertionError("must not yield")
        assert "boom: no upstream" in str(info.value)


class TestCli:
    @pytest.fixture
    def repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        monkeypatch.chdir(tmp_path)
        for name in (
            "GITLAB_CI",
            "GITHUB_ACTIONS",
            "CI_MERGE_REQUEST_IID",
            "GITHUB_EVENT_PATH",
            "GITHUB_REF",
            "REPOACH_PR_NUMBER",
            "REPOACH_ADVISORY_POST",
        ):
            monkeypatch.delenv(name, raising=False)
        return tmp_path

    def test_outside_a_merge_request_is_a_green_named_skip(self, repo: Path) -> None:
        result = CliRunner().invoke(app, ["ci", "review", "--forge", "gitlab"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["outcome"] == "skipped"
        assert "CI_MERGE_REQUEST_IID" in payload["reason"]

    def test_unknown_forge_flag_is_a_config_error(self, repo: Path) -> None:
        result = CliRunner().invoke(app, ["ci", "review", "--forge", "bitbucket", "--strict"])
        assert result.exit_code == 2
        assert json.loads(result.stdout)["outcome"] == "config_error"

    def test_post_and_dry_run_strict(self, repo: Path) -> None:
        result = CliRunner().invoke(app, ["ci", "review", "--post", "--dry-run", "--strict"])
        assert result.exit_code == 2
        assert "mutually exclusive" in json.loads(result.stdout)["reason"]

    def test_help_lists_the_exit_codes(self) -> None:
        result = CliRunner().invoke(app, ["ci", "review", "--help"])
        assert result.exit_code == 0
        assert "REQUEST_CHANGES" in result.stdout


def _gone(pid: int, within_s: float = 5.0) -> bool:
    """True once ``pid`` no longer exists, or is a zombie awaiting its reaper."""
    deadline = time.monotonic() + within_s
    status = Path(f"/proc/{pid}/status")
    while time.monotonic() < deadline:
        try:
            state = status.read_text(encoding="utf-8")
        except FileNotFoundError:
            return True
        if "\nState:\tZ" in state:
            return True
        time.sleep(0.05)
    return False


class TestChildRobustness:
    def _plan(self, strict: bool = True) -> CiReviewPlan:
        planned = plan_ci_review({**GITLAB_ENV, **GATEWAY_VARS}, strict=strict)
        assert isinstance(planned, CiReviewPlan)
        return planned

    def test_timeout_kills_the_whole_process_group(self, tmp_path: Path) -> None:
        grandchild = tmp_path / "grandchild.pid"
        python = _fake_python(
            tmp_path,
            "import subprocess, time\n"
            "child = subprocess.Popen(['sleep', '300'])\n"
            f"open({str(grandchild)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(300)\n",
        )
        fake = FakeRun()
        report = run_ci_review(
            self._plan(),
            {**_interpreter_env(), **GITLAB_ENV, **GATEWAY_VARS},
            pr_state=fake.pr_state,
            review_runner=subprocess_review_runner(tmp_path, python, timeout_s=1.5),
            gateway_starter=fake.gateway,
            bot_username="repoach-bot",
        )
        assert report.outcome == Outcome.TIMEOUT
        assert report.exit_code == 3
        pid = int(grandchild.read_text(encoding="utf-8"))
        assert _gone(pid)

    def test_unknown_state_is_a_named_skip(self) -> None:
        fake = FakeRun(state="unknown")
        report = _run({**GITLAB_ENV, **GATEWAY_VARS}, fake)
        assert report.outcome == Outcome.SKIPPED
        assert "state 'unknown'" in report.reason
        assert fake.review_envs == []

    def test_unparseable_stdout_is_carried_in_detail(self) -> None:
        noise = "Traceback (most recent call last):\n" + "x" * 500
        report = _run({**GITLAB_ENV, **GATEWAY_VARS}, FakeRun(rc=1, stdout=noise), strict=True)
        assert report.outcome == Outcome.INTERNAL_ERROR
        assert report.detail == noise[:200]
        assert report.exit_code == 1

    @pytest.mark.parametrize(("strict", "code"), [(False, 0), (True, 1)])
    def test_any_exception_is_one_json_internal_error(
        self, monkeypatch: pytest.MonkeyPatch, strict: bool, code: int
    ) -> None:
        def explode(*args: object, **kwargs: object) -> CiReviewReport:
            raise RuntimeError("disk on fire")

        monkeypatch.setattr("repoach.ci_entry.cli.review_from_env", explode)
        args = ["ci", "review", *(["--strict"] if strict else [])]
        result = CliRunner().invoke(app, args)
        assert result.exit_code == code
        payload = json.loads(result.stdout)
        assert payload["outcome"] == "internal_error"
        assert payload["reason"] == "RuntimeError: disk on fire"
        assert "Traceback" not in result.stdout


class TestAgentMemoryInCi:
    def test_ci_turns_memory_off_with_a_note(self) -> None:
        fake = FakeRun()
        report = _run({**GITLAB_ENV, **GATEWAY_VARS}, fake)
        assert report.notes == [
            "agent memory off: CI job (the agentmemory service is a developer-machine service)"
        ]
        env = fake.review_envs[0]
        assert env["REPOACH_REVIEW_MEMORY_ENABLED"] == "false"
        assert env["REPOACH_BUILDER_MEMORY_ENABLED"] == "false"
        assert env["REPOACH_REVIEW_LESSONS_ENABLED"] == "false"

    def test_an_explicit_switch_is_respected(self) -> None:
        fake = FakeRun()
        env = {**GITLAB_ENV, **GATEWAY_VARS, "REPOACH_REVIEW_MEMORY_ENABLED": "true"}
        report = _run(env, fake)
        assert report.notes == []
        assert fake.review_envs[0]["REPOACH_REVIEW_MEMORY_ENABLED"] == "true"
        assert "REPOACH_BUILDER_MEMORY_ENABLED" not in fake.review_envs[0]

    @pytest.mark.parametrize(("reachable", "off"), [(False, True), (True, False)])
    def test_outside_ci_memory_follows_reachability(self, reachable: bool, off: bool) -> None:
        env = {"GH_TOKEN": "t", "REPOACH_AGENTMEMORY_URL": "http://memory:3111", **GATEWAY_VARS}
        planned = plan_ci_review(env, pr_override=5, forge_override="github")
        assert isinstance(planned, CiReviewPlan)
        fake = FakeRun()
        probed: list[str] = []

        def probe(url: str) -> bool:
            probed.append(url)
            return reachable

        report = run_ci_review(
            planned,
            env,
            pr_state=fake.pr_state,
            review_runner=fake.runner,
            gateway_starter=fake.gateway,
            memory_probe=probe,
        )
        assert probed == ["http://memory:3111"]
        assert (report.notes == ["agent memory off: http://memory:3111 unreachable"]) is off
        assert ("REPOACH_REVIEW_MEMORY_ENABLED" in fake.review_envs[0]) is off

    def test_memory_reachable_on_a_closed_port(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        assert memory_reachable(f"http://127.0.0.1:{port}", timeout_s=0.5) is False


def test_python_dash_m_repoach_has_no_runpy_warning(tmp_path: Path) -> None:
    src = Path(__file__).resolve().parents[2] / "src"
    completed = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-m", "repoach", "--version"],
        capture_output=True,
        text=True,
        env={**_interpreter_env(), "HOME": str(tmp_path), "PYTHONPATH": str(src)},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("repoach ")
    assert "RuntimeWarning" not in completed.stderr


INTERPRETER_VARS: dict[str, str] = {
    "LD_LIBRARY_PATH": "/opt/hostedtoolcache/Python/3.13.0/x64/lib",
    "PYTHONHOME": "/opt/python",
    "VIRTUAL_ENV": "/work/.venv",
    "TMPDIR": "/runner/tmp",
    "SSL_CERT_FILE": "/etc/ssl/cert.pem",
    "SSL_CERT_DIR": "/etc/ssl/certs",
    "REQUESTS_CA_BUNDLE": "/etc/ssl/bundle.pem",
    "pythonLocation": "/opt/hostedtoolcache/Python/3.13.0/x64",
    "Python_ROOT_DIR": "/opt/hostedtoolcache/Python/3.13.0/x64",
}
"""What a CI runner sets for its interpreter to exec (actions/setup-python shape)."""


def test_gateway_env_keeps_what_the_interpreter_needs_and_never_ld_preload() -> None:
    parent = {
        "PATH": "/usr/bin",
        **INTERPRETER_VARS,
        "LD_PRELOAD": "/tmp/evil.so",
        "REPOACH_ANTHROPIC_API_KEY": "k",
    }
    child = gateway_env(parent, port=1, auth_token="t")
    assert {name: child.get(name) for name in INTERPRETER_VARS} == INTERPRETER_VARS
    assert "LD_PRELOAD" not in child


class TestInProcessProviders:
    """SP-UNIVERSAL-PROVIDERS: an explicit kind reviews in-process, no gateway started."""

    def test_claude_code_needs_no_key_and_no_gateway(self) -> None:
        fake = FakeRun()
        report = _run({**GITLAB_ENV, "REPOACH_PROVIDER": "claude_code"}, fake)
        assert (report.outcome, report.provider_mode) == (Outcome.REVIEWED, "claude_code")
        assert fake.gateway_envs == []
        env = fake.review_envs[0]
        assert env["REPOACH_PROVIDER"] == "claude_code"
        assert "REPOACH_LLM_PROXY_BASE_URL" not in env

    def test_claude_code_wins_over_a_gateway(self) -> None:
        fake = FakeRun()
        report = _run({**GITLAB_ENV, **GATEWAY_VARS, "REPOACH_PROVIDER": "claude_code"}, fake)
        assert report.provider_mode == "claude_code"
        assert fake.gateway_envs == []

    def test_anthropic_keeps_the_key_in_the_review(self) -> None:
        fake = FakeRun()
        report = _run({**GITLAB_ENV, **DIRECT_VARS, "REPOACH_PROVIDER": "anthropic"}, fake)
        assert (report.outcome, report.provider_mode) == (Outcome.REVIEWED, "anthropic")
        assert fake.gateway_envs == []
        env = fake.review_envs[0]
        assert env["REPOACH_ANTHROPIC_API_KEY"] == "sk-ant-secret"
        assert env["REPOACH_PROVIDER"] == "anthropic"

    def test_anthropic_without_a_key_is_a_named_skip(self) -> None:
        report = _run({**GITLAB_ENV, "REPOACH_PROVIDER": "anthropic"}, FakeRun())
        assert report.outcome == Outcome.SKIPPED
        assert "provider anthropic" in report.reason

    def test_kind_from_repoach_toml(self) -> None:
        fake = FakeRun()
        planned = plan_ci_review(GITLAB_ENV, provider_kind="claude_code")
        assert isinstance(planned, CiReviewPlan)
        assert planned.provider_mode == "claude_code"
        assert fake.gateway_envs == []
