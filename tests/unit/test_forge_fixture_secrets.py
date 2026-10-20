"""No forge fixture carries a secret, and recorded answers keep only fields the forges read.

A recorded ``projects/:id`` answer once carried GitLab's
``runners_token``; push protection refused the branch. Fixtures are now
redacted at record time to :data:`~repoach.forge.recording.RECORDED_FIELDS`
and this test pins it for every file under ``tests/fixtures/forge``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoach.forge.recording import (
    RECORDED_FIELDS,
    decode_json_values,
    find_secrets,
    redact_stdout,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "forge"
ALL = sorted(FIXTURES.glob("*/*.json"))


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def _stdouts(data: dict[str, object]) -> list[str]:
    entries = data["calls"] if "calls" in data else list(data.values())
    return [str(e["stdout"]) for e in entries if isinstance(e, dict) and "stdout" in e]


@pytest.mark.parametrize("path", ALL, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_fixture_carries_no_secret(path: Path) -> None:
    text = path.read_text()
    assert find_secrets(text) == []
    for stdout in _stdouts(json.loads(text)):
        assert find_secrets(stdout) == []


@pytest.mark.parametrize("path", ALL, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_recorded_answers_keep_only_read_fields(path: Path) -> None:
    data = json.loads(path.read_text())
    if "calls" in data and not str(data.get("source", "")).startswith("recorded"):
        pytest.skip("synthetic fixture")
    for stdout in _stdouts(data):
        for value in decode_json_values(stdout) or []:
            assert _keys(value) <= RECORDED_FIELDS


def test_redaction_drops_runner_token_and_emails() -> None:
    answer = json.dumps(
        {
            "id": 1,
            "merge_method": "merge",
            "runners_token": "GR1348941" + "x" * 20,
            "commit": {"author_email": "someone@example.org"},
            "user": {"login": "bot", "node_id": "MDQ6VXNlcjE="},
        }
    )
    assert find_secrets(answer)
    redacted = redact_stdout(answer)
    assert find_secrets(redacted) == []
    assert json.loads(redacted) == {"id": 1, "merge_method": "merge", "user": {"login": "bot"}}
