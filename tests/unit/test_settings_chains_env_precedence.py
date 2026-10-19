"""Config env-file precedence.

The MAIN settings (``core.config``) no longer read ``chains.env`` nor the
host repository's ``.env`` (SP-UNIVERSAL-INSTALL G5): they read
``$XDG_CONFIG_HOME/repoach/env`` then ``<project>/.repoach.env``, the
process environment winning over both. They have no ``MODEL_*`` fields,
so the capability chains were never theirs to read.

The PROXY settings flipped under SP-CHAINS-SINGLE-SOURCE (2026-06-21):
``chains.env`` is read LAST so it is AUTHORITATIVE for the four capability
chains — a per-machine ``.env`` can no longer shadow the canonical file.
"""

from __future__ import annotations

from repoach.core.config import _anchored_env_files
from repoach.llm_proxy.config import settings as proxy_settings_module


def test_core_settings_env_files_are_user_then_project() -> None:
    """User-level ``env`` precedes the project's ``.repoach.env`` so the project wins."""
    user_env, project_env = _anchored_env_files()
    assert user_env.name == "env"
    assert user_env.parent.name == "repoach"
    assert project_env.name == ".repoach.env"
    assert user_env.is_absolute(), "anchored paths must be absolute"
    assert project_env.is_absolute(), "anchored paths must be absolute"


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
