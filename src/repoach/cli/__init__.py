"""Top-level Typer CLI registered as ``repoach`` (see ``pyproject.toml``).

Importing the CLI routes structlog's default output to stderr before any
command runs, so every command's stdout carries its result alone
(SP-CLI-STDOUT-ONE-JSON). Module loggers are lazy proxies that read the
factory at first use, so the call may follow the imports.
"""

from ..core.logging import route_default_logs_to_stderr
from .main import app

route_default_logs_to_stderr()

__all__ = ["app"]
