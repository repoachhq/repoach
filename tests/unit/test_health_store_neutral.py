"""Tests for the neutralized health store (SP-HEALTH-STORE-NEUTRALIZE).

Asserts the cycle is broken — importing the health store pulls in neither
``llm_proxy`` nor ``review`` — and that the moved store still round-trips
and the back-compat shim re-exports.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from ferova.health.model_health import ModelHealth
from ferova.health.store import fetch_probes, record_probes


def test_health_store_imports_no_llm_proxy_or_review() -> None:
    code = (
        "import sys, ferova.health.store, ferova.health.model_health\n"
        "bad = [m for m in sys.modules "
        "if m.startswith('ferova.llm_proxy') or m.startswith('ferova.review')]\n"
        "assert not bad, bad\n"
        "print('clean')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout


def test_record_and_fetch_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "probes.db"
    probes = [
        ModelHealth("haiku", "mistralai/mistral-small-4-119b-2603", "error", None, 0, "http=410"),
        ModelHealth("opus", "mistralai/mistral-medium-3.5-128b", "ok", 2.4, 2, "ok"),
    ]
    written = record_probes(db, probes, recorded_at=datetime(2026, 6, 21, tzinfo=UTC))
    assert written == 2

    rows = fetch_probes(db, tier="haiku")
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert rows[0].detail == "http=410"


def test_shim_reexports_from_health_store() -> None:
    from ferova.review import chain_health_store

    assert chain_health_store.fetch_probes is fetch_probes
    assert chain_health_store.record_probes is record_probes
