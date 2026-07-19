"""Unit tests for SP-DEV-RUFF-UNSAFE-FIXES — build gate auto-resolves unsafe-fixable nits.

A non-autofixable-by-default ruff rule (SIM102, collapsible nested
ifs) stalled an autonomous dispatch twice: the model could not
hand-restructure it. The build gate now passes ``--unsafe-fixes`` so
ruff applies its own known fix; the Coder loop keeps the safe default.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.coder_loop import run_ruff_gate

_NESTED_IF = (
    '"""Module with a collapsible nested if (SIM102)."""\n'
    "\n"
    "\n"
    "def f(a: bool, b: bool) -> int:\n"
    '    """Return 1 when both hold."""\n'
    "    if a:\n"
    "        if b:\n"
    "            return 1\n"
    "    return 0\n"
)


_RUFF_CONFIG = '[tool.ruff.lint]\nselect = ["SIM"]\n'


def _seed(repo: Path) -> None:
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(_RUFF_CONFIG, encoding="utf-8")
    (repo / "src" / "sim_mod.py").write_text(_NESTED_IF, encoding="utf-8")


def test_build_gate_resolves_sim102_with_unsafe_fixes(tmp_path: Path) -> None:
    _seed(tmp_path)
    ok, tail = run_ruff_gate(tmp_path, unsafe_fixes=True)
    assert ok is True, tail
    collapsed = (tmp_path / "src" / "sim_mod.py").read_text(encoding="utf-8")
    assert "if a and b:" in collapsed


def test_coder_default_leaves_sim102_unfixed(tmp_path: Path) -> None:
    _seed(tmp_path)
    ok, tail = run_ruff_gate(tmp_path)
    assert ok is False
    assert "SIM102" in tail
