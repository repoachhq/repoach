"""The security posture cites only names that exist (SP-RECORDER-REDACTS-BY-CLASS, PR #319).

A renamed constant left behind in ``docs/security_posture.md`` reads as a second table
that can drift; this suite refuses it.
"""

from __future__ import annotations

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
