"""Project-declared gates (SP-UNIVERSAL-GATES).

* :mod:`.config` — :class:`GatesConfig`, the knobs read from ``repoach.toml``;
* :mod:`.languages` — language detection and the per-language table;
* :mod:`.commands` — the project's ``lint`` / ``test`` / ``gate`` commands.
"""

from __future__ import annotations

from .commands import run_lint_gate, run_project_gate, run_test_gate
from .config import GatesConfig, applicable, current_gates_config, load_gates_config
from .languages import LANGUAGE_TABLE, Language, diff_languages, language_of

__all__ = [
    "LANGUAGE_TABLE",
    "GatesConfig",
    "Language",
    "applicable",
    "current_gates_config",
    "diff_languages",
    "language_of",
    "load_gates_config",
    "run_lint_gate",
    "run_project_gate",
    "run_test_gate",
]
