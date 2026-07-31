"""Guard tests for the hand-maintained PyPI publish workflow.

`.github/workflows/publish.yml` is bot-forbidden and publishes the package to a
public index, so its trust-critical wiring must not silently regress: it fires
only on a version tag, uses OIDC trusted publishing (never a stored token), and
pins every third-party action to an immutable commit SHA (SP-CI-SUPPLY-CHAIN-
HARDEN G1).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_PUBLISH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "publish.yml"


def _workflow() -> dict:
    return yaml.safe_load(_PUBLISH.read_text(encoding="utf-8"))


def test_publish_triggers_only_on_version_tags() -> None:
    workflow = _workflow()
    on_block = workflow[True]
    assert on_block["push"]["tags"] == ["v*"]
    assert "pull_request" not in on_block


def test_publish_job_uses_oidc_trusted_publishing() -> None:
    job = _workflow()["jobs"]["build-and-publish"]
    assert job["permissions"]["id-token"] == "write"
    steps = job["steps"]
    publish = [s for s in steps if str(s.get("uses", "")).startswith("pypa/gh-action-pypi-publish")]
    assert len(publish) == 1


def test_publish_runs_on_the_configurable_runner() -> None:
    job = _workflow()["jobs"]["build-and-publish"]
    assert job["runs-on"] == "${{ vars.REPOACH_RUNNER || 'self-hosted' }}"


def test_every_action_is_pinned_to_a_commit_sha() -> None:
    sha_pin = re.compile(r"^[^@]+@[0-9a-f]{40}(\s+#.*)?$")
    for line in _PUBLISH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("uses:") or stripped.startswith("- uses:"):
            ref = stripped.split("uses:", 1)[1].strip()
            assert sha_pin.match(ref), f"action not pinned to a 40-hex SHA: {ref}"
