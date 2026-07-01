"""SP-PLANNER-CC-EXPLORE — the claude -p delegated-exploration backend.

Pins :func:`run_cc_exploration` against a faked ``subprocess.run``:
the read-only tool allowlist + --add-dir are passed only when
exploring, the JSON envelope's ``result`` becomes the text, and every
failure mode (non-zero exit, timeout, non-JSON envelope, the CLI's
own ``is_error``) is reported via ``is_error`` and never raised.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ferova.review.planner_cc import run_cc_exploration


def _envelope(result: str, *, is_error: bool = False, num_turns: int = 3) -> str:
    return json.dumps(
        {
            "type": "result",
            "is_error": is_error,
            "result": result,
            "num_turns": num_turns,
            "duration_ms": 7777,
        }
    )


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _run_capturing(stdout: str, **kw) -> tuple[object, list[str]]:
    res, cmd, _cwd = _run_capturing_cwd(stdout, **kw)
    return res, cmd


def _run_capturing_cwd(stdout: str, **kw) -> tuple[object, list[str], object]:
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return _completed(stdout, **kw)

    with patch("ferova.review.planner_cc.subprocess.run", side_effect=fake_run):
        res = run_cc_exploration(
            prompt="plan it", repo_root=Path("/repo"), model="sonnet", cli_path="claude-stub"
        )
    return res, captured["cmd"], captured["cwd"]


class TestSuccess:
    def test_result_field_becomes_text(self) -> None:
        res, _ = _run_capturing(_envelope('{"spec_id": "SP-X"}'))
        assert res.is_error is False
        assert res.text == '{"spec_id": "SP-X"}'
        assert res.num_turns == 3
        assert res.duration_ms == 7777

    def test_exploration_passes_readonly_tools_and_add_dir(self) -> None:
        _, cmd = _run_capturing(_envelope("{}"))
        assert "--allowedTools" in cmd
        idx = cmd.index("--allowedTools")
        assert cmd[idx + 1] == "Read,Glob,Grep,LS"
        assert "--add-dir" in cmd
        assert "Write" not in " ".join(cmd)
        assert "Edit" not in " ".join(cmd)
        assert "Bash" not in " ".join(cmd)
        assert cmd[0] == "claude-stub"
        assert cmd[1] == "-p"
        assert "plan it" in cmd[2]
        assert "/repo" in cmd[2]

    def test_runs_from_isolated_cwd_never_the_repo(self) -> None:
        _, _cmd, cwd = _run_capturing_cwd(_envelope("{}"))
        assert cwd is not None
        assert str(cwd) != "/repo"
        assert "ferova_planner_cc_" in str(cwd)

    def test_refinement_omits_tools_and_add_dir(self) -> None:
        def fake_run(cmd, **kwargs):
            fake_run.cmd = cmd
            return _completed(_envelope("{}"))

        with patch("ferova.review.planner_cc.subprocess.run", side_effect=fake_run):
            run_cc_exploration(
                prompt="fix",
                repo_root=Path("/repo"),
                model="sonnet",
                allow_tools=False,
                cli_path="claude-stub",
            )
        assert "--allowedTools" not in fake_run.cmd
        assert "--add-dir" not in fake_run.cmd


class TestEnvScrubbing:
    def test_default_env_strips_sharp_secrets(self, monkeypatch) -> None:
        monkeypatch.setenv("FEROVA_NVIDIA_NIM_API_KEY", "nvapi-secret")
        monkeypatch.setenv("FEROVA_DB_PATH", "/tmp/db")
        monkeypatch.setenv("PATH", "/usr/bin")
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return _completed(_envelope("{}"))

        with patch("ferova.review.planner_cc.subprocess.run", side_effect=fake_run):
            run_cc_exploration(
                prompt="x", repo_root=Path("/repo"), model="sonnet", cli_path="claude-stub"
            )

        env = captured["env"]
        assert env is not None
        assert "PATH" in env
        assert not any(k.startswith("FEROVA_") for k in env)

    def test_explicit_env_is_passed_through(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return _completed(_envelope("{}"))

        with patch("ferova.review.planner_cc.subprocess.run", side_effect=fake_run):
            run_cc_exploration(
                prompt="x",
                repo_root=Path("/repo"),
                model="sonnet",
                cli_path="claude-stub",
                env={"HOME": "/home/x"},
            )

        assert captured["env"] == {"HOME": "/home/x"}


class TestFailureModes:
    def test_nonzero_exit_is_error(self) -> None:
        res, _ = _run_capturing("boom", returncode=1, stderr="upstream 500")
        assert res.is_error is True
        assert "exited 1" in res.error

    def test_cli_is_error_envelope(self) -> None:
        res, _ = _run_capturing(_envelope("rate limited", is_error=True))
        assert res.is_error is True
        assert "is_error" in res.error

    def test_non_json_envelope(self) -> None:
        res, _ = _run_capturing("not json at all")
        assert res.is_error is True
        assert "non-JSON" in res.error

    def test_timeout_is_error_not_raised(self) -> None:
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 600)

        with patch("ferova.review.planner_cc.subprocess.run", side_effect=fake_run):
            res = run_cc_exploration(
                prompt="x", repo_root=Path("/repo"), model="sonnet", cli_path="claude-stub"
            )
        assert res.is_error is True
        assert "timed out" in res.error

    def test_spawn_oserror_is_error(self) -> None:
        with patch(
            "ferova.review.planner_cc.subprocess.run",
            side_effect=OSError("no such binary"),
        ):
            res = run_cc_exploration(
                prompt="x", repo_root=Path("/repo"), model="sonnet", cli_path="missing"
            )
        assert res.is_error is True
        assert "could not be spawned" in res.error
