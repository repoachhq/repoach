"""Regression test for the ``cache_logger_on_first_use`` test-order flake.

Reproduces, mechanically and against the real
:func:`repoach.core.logging.configure_logging`, the freeze/replace
sequence described in ``docs/specs/2026-07-24_SP-STRUCTLOG-CACHE-TEST-FIXTURE_centralize-logger-cache-disable-fixture.md``:

1. An earlier ``CliRunner``-driven test calls ``configure_logging()``,
   which unconditionally sets ``cache_logger_on_first_use=True`` and
   installs a brand-new processors list.
2. A later test performs the module logger's first-ever real log call
   while that flag is still ``True``: ``structlog``'s
   ``BoundLoggerLazyProxy.bind()`` permanently freezes itself to a
   closure holding a reference to whichever processors list is current
   at that instant.
3. A further ``configure_logging()`` call installs yet another,
   unrelated processors list object; the frozen logger from step 2 keeps
   writing into the now-orphaned earlier list.
4. ``structlog.testing.capture_logs()`` mutates the CURRENT processors
   list in place, but the frozen logger never touches it — its events
   silently vanish from ``capture_logs()``'s output.

These four steps are modelled as four ordered test functions sharing one
module-level logger, and MUST run serially (no ``-n auto``) to reproduce
the freeze in a single process: ``pytest tests/unit/test_logging_cache_disabled_for_tests.py``.
Under ``pytest -n auto --dist worksteal`` (the CI / ``ci_local.sh``
invocation), ``pytest-xdist`` may distribute these test items across
worker processes with independent ``structlog`` global state, which can
mask (never falsely reproduce) the flake for an unrelated reason; the
fix under test — the autouse fixture in ``tests/conftest.py`` resetting
``cache_logger_on_first_use=False`` before EVERY test — makes step 4
pass regardless of which worker or order runs it, so this file stays
green under both invocations.

Pre-fix (no ``tests/conftest.py``), the fourth test observes an empty
``capture_logs()`` list and fails; post-fix, the shared fixture resets
the flag immediately before step 2's first real use, so the logger
never freezes and the fourth test observes the captured event.
"""

from __future__ import annotations

import structlog
from structlog.testing import capture_logs

from repoach.core.logging import configure_logging

_target_log = structlog.get_logger("test.logging_cache_disabled_for_tests")


def test_1_earlier_cli_invoking_test_configures_logging() -> None:
    configure_logging()


def test_2_earlier_non_capture_use_of_target_logger() -> None:
    _target_log.info("warmup_event_outside_capture")


def test_3_another_cli_invoking_test_reconfigures() -> None:
    configure_logging()


def test_4_capture_logs_sees_the_captured_event() -> None:
    with capture_logs() as entries:
        _target_log.info("captured_event")

    assert entries != []
    assert any(entry["event"] == "captured_event" for entry in entries)
