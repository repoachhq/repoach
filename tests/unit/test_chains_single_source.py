"""Tests for SP-CHAINS-SINGLE-SOURCE.

chains.env is authoritative for the three capability chains: it is read
last in the proxy env_file order, the FEROVA_MODEL_* alias is gone, and a
per-machine .env can no longer shadow the canonical file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoach.llm_proxy.config.settings import _LEGACY_TO_FEROVA_ALIAS, Settings, _env_files

_CHAIN_KEYS = ("MODEL_OPUS", "MODEL_SONNET", "MODEL_HAIKU")


def _clear_chain_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (*_CHAIN_KEYS, "FCC_ENV_FILE"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"FEROVA_{key}", raising=False)


def test_chains_env_is_read_last(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FCC_ENV_FILE", raising=False)
    assert _env_files()[-1] == Path("chains.env")


def test_sharp_model_alias_is_dropped() -> None:
    assert not [key for key in _LEGACY_TO_FEROVA_ALIAS if key.startswith("MODEL_")]


def test_sharp_model_env_no_longer_populates_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_chain_env(monkeypatch)
    monkeypatch.setenv("FEROVA_MODEL_OPUS", "nvidia_nim/should-be-ignored")
    assert Settings(_env_file=None).model_opus is None


def test_bare_model_env_populates_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_chain_env(monkeypatch)
    monkeypatch.setenv("MODEL_OPUS", "nvidia_nim/x,claude_code/opus")
    assert Settings(_env_file=None).model_opus == "nvidia_nim/x,claude_code/opus"


def test_chains_env_wins_over_dotenv_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_chain_env(monkeypatch)
    (tmp_path / "chains.env").write_text("MODEL_OPUS=nvidia_nim/canonical-opus,claude_code/opus\n")
    (tmp_path / ".env").write_text(
        "FEROVA_MODEL_OPUS=nvidia_nim/stale-shadow\nMODEL_OPUS=nvidia_nim/stale-bare\n"
    )
    monkeypatch.chdir(tmp_path)
    assert Settings().model_opus == "nvidia_nim/canonical-opus,claude_code/opus"
