"""SP-CONSISTENCY-SWEEP G2 — the parse-attempts knob reads through Settings.

Audit 2026-07-13 finding C2: ``review/planner.py`` read
``REPOACH_PLANNER_PARSE_ATTEMPTS`` via a raw ``os.environ.get`` call,
bypassing ``core/config.py``'s ``Settings`` — undocumented, and not
injectable the way every other repoach knob is. ``_parse_attempts`` now
reads ``settings.planner_parse_attempts``; A3 (tests inject via the
settings object rather than ``os.environ``) is pinned here by
constructing a :class:`Settings` override directly and passing it in,
never touching the process environment.
"""

from __future__ import annotations

from repoach.core.config import Settings
from repoach.review.planner import _parse_attempts


def _settings(raw: str) -> Settings:
    return Settings(
        _env_file=(),
        llm_proxy_base_url="http://settings-knob.test.invalid:9999",
        planner_parse_attempts=raw,
    )


def test_parse_attempts_via_settings() -> None:
    """The override flows through ``Settings``, not the environment.

    Pre-fix, ``_parse_attempts`` ignored any ``Settings`` override
    entirely (it read ``os.environ`` directly), so an injected
    ``Settings(planner_parse_attempts=...)`` had no effect and every
    case below collapsed to the built-in default of 5 — this test
    fails on that pre-change code.
    """
    assert _parse_attempts(_settings("3")) == 3
    assert _parse_attempts(_settings("")) == 5
    assert _parse_attempts(_settings("0")) == 1
    assert _parse_attempts(_settings("-9")) == 1
    assert _parse_attempts(_settings("not-a-number")) == 5
