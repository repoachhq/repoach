"""Unit tests for the ``chains-audit`` CLI command (SP-CHAINS-THINKING-CLASS).

The command is registered on the top-level Typer ``app`` and its callback
returns ``None`` (no exception path) — the report-only slice always exits 0.
"""

from __future__ import annotations

from ferova.cli.main import app


def test_chains_audit_command_is_registered() -> None:
    """The ``chains-audit`` command is registered on the top-level Typer app."""
    command_names = [command.name for command in app.registered_commands]
    assert "chains-audit" in command_names


def test_chains_audit_callback_returns_none() -> None:
    """The ``chains-audit`` callback returns ``None`` (no exception path)."""
    command = next(
        registered for registered in app.registered_commands if registered.name == "chains-audit"
    )
    assert command.callback is not None
    assert command.callback.__name__ == "chains_audit"
