"""Settings-sourced automerge CI-gate wait/poll knobs (SP-AUTOMERGE-EVENT-DRIVEN G1).

``FEROVA_AUTOMERGE_CI_WAIT_SECONDS`` / ``FEROVA_AUTOMERGE_CI_POLL_INTERVAL``
tune the total CI-gate wait budget and the poll cadence within it.  Always
build ``Settings(_env_file=None)`` so this test is immune to env-file
anchoring changes landing in the same region (SP-CONFIG-ENV-ANCHOR).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ferova.core.config import Settings


def test_settings_env_overrides_wait_and_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """FEROVA_AUTOMERGE_CI_* env vars override the defaults, else 720/30."""
    monkeypatch.setenv("FEROVA_AUTOMERGE_CI_WAIT_SECONDS", "0")
    monkeypatch.setenv("FEROVA_AUTOMERGE_CI_POLL_INTERVAL", "5")
    overridden = Settings(_env_file=None)
    assert overridden.automerge_ci_wait_seconds == 0
    assert overridden.automerge_ci_poll_interval == 5

    monkeypatch.delenv("FEROVA_AUTOMERGE_CI_WAIT_SECONDS", raising=False)
    monkeypatch.delenv("FEROVA_AUTOMERGE_CI_POLL_INTERVAL", raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.automerge_ci_wait_seconds == 720
    assert defaults.automerge_ci_poll_interval == 30

    monkeypatch.setenv("FEROVA_AUTOMERGE_CI_WAIT_SECONDS", "-1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.delenv("FEROVA_AUTOMERGE_CI_WAIT_SECONDS", raising=False)

    monkeypatch.setenv("FEROVA_AUTOMERGE_CI_POLL_INTERVAL", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
