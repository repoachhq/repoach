"""The security posture cites only names that exist (SP-RECORDER-REDACTS-BY-CLASS, PR #319).

A renamed constant left behind in ``docs/security_posture.md`` reads as a second table
that can drift; this suite refuses it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_security_posture_names_only_symbols_the_code_defines() -> None:
    """PR #319: every upper-case name the posture cites in backticks is the code's, a workflow's or a file's.

    ``LOGIN`` is the one exception, named: a key the posture says no reader reads.
    """
    posture = (ROOT / "docs" / "security_posture.md").read_text(encoding="utf-8")
    cited = set(re.findall(r"`([A-Z_][A-Z0-9_]{3,})`", posture)) - {"LOGIN"}
    sources = [
        *(ROOT / "src").rglob("*.py"),
        *(ROOT / "scripts").glob("*.py"),
        *(ROOT / ".github").rglob("*.yml"),
    ]
    code = {name for p in sources for name in re.findall(r"\b[A-Z_][A-Z0-9_]{3,}\b", p.read_text())}
    files = {p.name for p in ROOT.iterdir()}
    assert {"PUBLIC_SUFFIXES", "PROVENANCE"} <= cited
    assert sorted(cited - code - files) == []


def test_the_gate_spec_home_path_rule_is_the_recorder_copy() -> None:
    """PR #319 round 6 (4684965373): SP-NO-PERSONAL-DATA-GATE D1 ``home_path`` spells ``HOME_PATH_RE``.

    The recorder holds the copy until the gate lands; the spec's pattern is the copy's, byte for
    byte, so a home-path form added to one is added to the other.
    """
    tree = ast.parse((ROOT / "src/repoach/forge/fixture_judge.py").read_text(encoding="utf-8"))
    copies = [
        node.value.args[0].value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "HOME_PATH_RE" for t in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.args[0], ast.Constant)
    ]
    assert len(copies) == 1
    spec = next((ROOT / "docs" / "specs").glob("2026-11-18_SP-NO-PERSONAL-DATA-GATE_*.md"))
    line = next(x for x in spec.read_text(encoding="utf-8").splitlines() if "`home_path`:" in x)
    assert line.split("`")[3] == copies[0]
