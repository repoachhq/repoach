"""Project-declared gates and language-aware verifiers (SP-UNIVERSAL-GATES).

* :mod:`.config` — :class:`GatesConfig`, the knobs read from ``repoach.toml``;
* :mod:`.languages` — language detection and the per-language table;
* :mod:`.commands` — the project's ``lint`` / ``test`` / ``gate`` commands;
* :mod:`.verifiers` — per-language file verifiers behind one interface;
* :mod:`.selectors` — pytest, Vitest, Go and ``cmd:`` test selectors;
* :mod:`.typescript` — tsconfig, Node tool and import resolution helpers.
"""

from __future__ import annotations

from .commands import run_lint_gate, run_project_gate, run_test_gate
from .config import GatesConfig, applicable, current_gates_config, load_gates_config
from .languages import LANGUAGE_TABLE, Language, diff_languages, language_of
from .selectors import TestSelector, parse_selector
from .verifiers import VerifierReport, VerifierStatus, run_verifiers

__all__ = [
    "LANGUAGE_TABLE",
    "GatesConfig",
    "Language",
    "TestSelector",
    "VerifierReport",
    "VerifierStatus",
    "applicable",
    "current_gates_config",
    "diff_languages",
    "language_of",
    "load_gates_config",
    "parse_selector",
    "run_lint_gate",
    "run_project_gate",
    "run_test_gate",
    "run_verifiers",
]
