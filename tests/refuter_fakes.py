"""Shared fixtures of the refuter suites: a tracked head tree, a judge reply, a finding.

The refuter reads its evidence with ``git grep`` over the tracked files of
a head tree, so every refuter test builds one; and its judge answers with
a column-one ``VERDICT:`` line. These helpers keep that shape in one place
for ``test_refuter_defers_unseen.py``, ``test_refuter_veto_names_real_identifiers.py``
and ``test_refuter_context_adapts_to_evidence.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from repoach.review.findings import ClaimType, Finding, Severity


def git_tree(root: Path, files: dict[str, str]) -> Path:
    """Write ``files`` under ``root`` and track them in a git work tree; return ``root``."""
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def judge_reply(verdict: str, reasoning: str) -> str:
    """Return a judge reply carrying ``verdict`` and ``reasoning`` on its ``VERDICT:`` line."""
    return "VERDICT: " + json.dumps({"verdict": verdict, "reasoning": reasoning})


def blocking_finding(
    claim: str,
    *,
    file: str,
    line: int,
    head: str,
    pr_number: int,
    claim_type: ClaimType = ClaimType.SECURITY,
) -> Finding:
    """Return a blocking finding a sentinel raised at ``head`` on ``file:line``."""
    return Finding(
        pr_number=pr_number,
        head_sha=head,
        round=1,
        finder="sentinel",
        claim_type=claim_type,
        severity=Severity.BLOCKING,
        file=file,
        line_start=line,
        line_end=line,
        claim=claim,
        evidence_pointer=f"{file}:{line}",
    )
