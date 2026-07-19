"""Config env-file precedence for chains.env.

The MAIN settings (``core.config``) keep ``env_file=("chains.env",
".env")`` — it has no ``MODEL_*`` fields, so the order is moot for the
chains and ``.env`` still wins for everything else.

The PROXY settings flipped under SP-CHAINS-SINGLE-SOURCE (2026-06-21):
``chains.env`` is read LAST so it is AUTHORITATIVE for the four capability
chains — a per-machine ``.env`` can no longer shadow the canonical file.
"""

from __future__ import annotations

from repoach.core.config import Settings
from repoach.llm_proxy.config import settings as proxy_settings_module


def test_core_settings_env_file_tuple_has_chains_env_first() -> None:
    """``chains.env`` must precede ``.env`` so .env wins on overlap."""
    env_file = Settings.model_config.get("env_file")
    assert env_file is not None
    # Pydantic-settings accepts str / Path / tuple thereof; we declare a tuple.
    assert isinstance(env_file, tuple), "env_file must be a tuple to load multiple files"
    paths = [str(p) for p in env_file]
    assert paths == ["chains.env", ".env"], (
        f"env_file order matters — chains.env first then .env on top, got {paths}"
    )


def test_proxy_env_files_reads_chains_env_after_dotenv() -> None:
    """The proxy reads chains.env AFTER .env so the canonical chains win.

    SP-CHAINS-SINGLE-SOURCE: chains.env is authoritative — a per-machine
    .env can no longer shadow MODEL_*.
    """
    files = [str(p) for p in proxy_settings_module._env_files()]
    assert "chains.env" in files, f"chains.env missing from proxy env_files: {files}"
    assert ".env" in files, f".env missing from proxy env_files: {files}"
    assert files.index(".env") < files.index("chains.env"), (
        f".env must precede chains.env so the canonical chains win, got order {files}"
    )
