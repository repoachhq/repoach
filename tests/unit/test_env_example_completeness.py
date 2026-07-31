"""Guard test: `.env.example` documents every REPOACH_ configuration knob.

A third party's first configuration surface is `.env.example`. When a new
`REPOACH_`-prefixed field is added to either Settings model but not to the
template, that knob becomes invisible — the onboarding gap this test closes. It
introspects both `pydantic-settings` models for their env-var names and asserts
each REPOACH_ one appears in `.env.example` (declared, whether active or a
commented default). The bare-name chain/provider keys that live authoritatively
in `chains.env` (SP-CHAINS-SINGLE-SOURCE) are the only sanctioned exclusions.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic_settings import BaseSettings

from repoach.core.config import Settings as CoreSettings
from repoach.llm_proxy.config.settings import Settings as ProxySettings

_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
_CHAINS_ENV_NAMES = {"MODEL_OPUS", "MODEL_SONNET", "MODEL_HAIKU", "NIM"}


def _env_names(model: type[BaseSettings]) -> set[str]:
    prefix = str(model.model_config.get("env_prefix", "") or "")
    names: set[str] = set()
    for field_name, field in model.model_fields.items():
        alias = field.validation_alias
        if alias is None:
            names.add(f"{prefix}{field_name}".upper())
            continue
        choices = getattr(alias, "choices", None)
        candidates = [str(c) for c in choices] if choices else [str(alias)]
        names.add(candidates[0].upper())
    return names


def _declared_in_example() -> set[str]:
    declared: set[str] = set()
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*#?\s*([A-Z][A-Z0-9_]+)=", line)
        if match:
            declared.add(match.group(1))
    return declared


def test_env_example_declares_every_repoach_settings_var() -> None:
    all_names = _env_names(CoreSettings) | _env_names(ProxySettings)
    repoach_names = {name for name in all_names if name.startswith("REPOACH_")}
    declared = _declared_in_example()
    missing = sorted(repoach_names - declared)
    assert not missing, f".env.example is missing REPOACH_ knobs: {missing}"


def test_env_example_has_no_stale_undeclared_names() -> None:
    all_names = _env_names(CoreSettings) | _env_names(ProxySettings)
    declared = _declared_in_example()
    stale = sorted(
        name for name in declared if name not in all_names and name not in _CHAINS_ENV_NAMES
    )
    assert not stale, f".env.example declares names that are not Settings fields: {stale}"
