"""Breadth-of-adoption test for the shared schema-init helper.

``SP-SCHEMA-INIT-RACE-GENERALIZE`` AC2. Asserts each of the nine
previously-unprotected sibling stores now routes its schema creation
through :func:`repoach.core.sqlite_schema_init.ensure_schema_created`
rather than a private, unprotected ``create_all(checkfirst=True)``
re-implementation, by monkeypatching each module's imported binding
with a call-recording spy and invoking its ``init_*_schema`` function.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from repoach.health import store as health_store
from repoach.llm_proxy.providers import cell_probe_store, effort_probe_store
from repoach.llm_proxy.routing import breaker_persist
from repoach.review import audit_log, persistence, planner_telemetry, spec_gate, stuck

_SIBLINGS: list[tuple[object, Callable[[Path], None], str]] = [
    (persistence, persistence.init_schema, "persistence.db"),
    (spec_gate, spec_gate.init_spec_coverage_schema, "spec_gate.db"),
    (audit_log, audit_log.init_audit_schema, "audit_log.db"),
    (health_store, health_store.init_nim_health_schema, "health_store.db"),
    (cell_probe_store, cell_probe_store.init_cell_health_schema, "cell_probe_store.db"),
    (effort_probe_store, effort_probe_store.init_cell_effort_schema, "effort_probe_store.db"),
    (breaker_persist, breaker_persist.init_breaker_state_schema, "breaker_persist.db"),
    (planner_telemetry, planner_telemetry.init_planner_telemetry_schema, "planner_telemetry.db"),
    (stuck, stuck.init_stuck_schema, "stuck.db"),
]


def _make_spy(
    calls: list[tuple[object, object]],
) -> Callable[[object, object], None]:
    """Build a call-recording stand-in for ``ensure_schema_created``."""

    def _spy(engine: object, metadata: object) -> None:
        calls.append((engine, metadata))

    return _spy


def test_all_nine_sibling_stores_call_the_shared_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every sibling module's ``init_*_schema`` calls the shared helper exactly once.

    For each ``(module, init_function, db_filename)`` tuple, patches
    the module's own imported ``ensure_schema_created`` name with a
    spy, invokes the init function against a fresh ``tmp_path``
    database, and asserts the spy recorded exactly one call -- proof
    the module no longer re-implements ``create_all(checkfirst=True)``
    on its own.
    """
    for module, init_function, db_filename in _SIBLINGS:
        calls: list[tuple[object, object]] = []
        monkeypatch.setattr(module, "ensure_schema_created", _make_spy(calls))

        db_path = tmp_path / db_filename
        init_function(db_path)

        assert len(calls) == 1, f"{module.__name__} did not call ensure_schema_created exactly once"
