"""Config env-file precedence for chains.env.

The MAIN settings (``core.config``) load ``chains.env`` then ``.env`` —
it has no ``MODEL_*`` fields, so the order is moot for the chains and
``.env`` still wins for everything else. SP-CONFIG-ENV-ANCHOR (2026-07-13)
anchored that pair to the repo root (``_anchored_env_files``) instead of
baking a CWD-relative tuple into ``model_config``, so the order guarantee
now lives in that helper rather than in a literal ``env_file=(...)``.

The PROXY settings flipped under SP-CHAINS-SINGLE-SOURCE (2026-06-21):
``chains.env`` is read LAST so it is AUTHORITATIVE for the four capability
chains — a per-machine ``.env`` can no longer shadow the canonical file.
"""

from __future__ import annotations

from repoach.core.config import _anchored_env_files
from repoach.llm_proxy.config import settings as proxy_settings_module


def test_core_settings_env_file_tuple_has_chains_env_first() -> None:
    """``chains.env`` must precede ``.env`` so .env wins on overlap."""
    chains_env, dotenv = _anchored_env_files()
    assert chains_env.name == "chains.env"
    assert dotenv.name == ".env"
    assert chains_env.is_absolute(), "anchored paths must be absolute (SP-CONFIG-ENV-ANCHOR)"
    assert dotenv.is_absolute(), "anchored paths must be absolute (SP-CONFIG-ENV-ANCHOR)"
    assert chains_env.parent == dotenv.parent, "both files anchor to the same root"


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
