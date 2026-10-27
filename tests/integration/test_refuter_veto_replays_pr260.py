"""Integration test: PR #260's findings 41 and 66, replayed, end ``refuted`` at the next head.

Measured on PR #260 (2026-10-26): both findings were fixed at the head,
the refuter refuted them, and the veto turned each refutation into
``needs judgement (evidence outside window)`` because the reasoning
named ``except``, ``Exception``, ``return``, ``None``,
``coverage_rows_at_head`` (41) or ``message``, ``https``, ``access``,
``token`` (66), each with more hits in the tree than the 50-hit cap.

This test rebuilds that evidence shape in a real git tree: a cited file
holding the fixed code, a definition module, and eighty files that each
use every one of those words, so every word is over the cap. It drives
the real :func:`~repoach.review.head_reverify.reverify_older_findings`
with the real ledger, the real evidence assembly (``git grep``) at the
40 000-token floor, and a judge that refutes with the measured
reasoning (SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS AC7). The ``2576-lines``
case pads the cited file to the real ``orchestrator.py`` length, past the
floor, with the claim's identifiers defined in ``spec_gate.py``: at the
default settings the context grows to serve both, and the ledger row
records the ``budget_used`` (G5, AC10).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from repoach.gates.config import REFUTER_CONTEXT_TOKENS_FLOOR, GatesConfig
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from repoach.review.head_reverify import reverify_older_findings

PR = 260
CITED = "src/repoach/review/orchestrator.py"
HEAD_A = "745e208" + "0" * 33

CITED_SOURCE = """\
from .spec_gate import _redacted, coverage_rows_at_head


class Orchestrator:
    def grade(self, pr_number, spec_id, head_sha):
        try:
            grading = coverage_rows_at_head(pr_number, head_sha)
            record_spec_coverages(grading.rows)
        except Exception as exc:
            _log.warning(
                "review_team.spec_coverage_failed",
                exc=type(exc).__name__,
                message=_redacted(str(exc))[:200],
                consequence="a not-covered row is recorded (fail closed)",
            )
            return self._record_grading_failure(pr_number, spec_id=spec_id, error=exc)
        return None

    def _record_grading_failure(self, pr_number, *, spec_id, error):
        return None
"""

SPEC_GATE_SOURCE = """\
def _redacted(text):
    return text


def _readable_base(base):
    return base


def fetch_base(base):
    return None


def specs_owning(paths):
    return ()


def coverage_rows_at_head(pr_number, head_sha):
    return None
"""

USER_SOURCE = """\
from repoach.review.spec_gate import coverage_rows_at_head


def use_{n}(message, token):
    try:
        coverage_rows_at_head(1, "https://x-access-token@host")
    except Exception:
        return None
    return message or token
"""

CLAIM_41 = (
    "CONFIRM: the excerpt at 1250-1257 shows `except Exception` → log `spec_coverage_skipped` "
    "→ `return None`, so no row is recorded. `coverage_rows_at_head` now runs `_readable_base`, "
    "`fetch_base` and `specs_owning` → `git show` inside this try. If any of them raises, an "
    "undeclared PR gets no coverage row, `spec_coverage_known=False`, and the gate merges with "
    "no coverage check."
)
REASONING_41 = (
    "Line 9 is now `except Exception as exc`, which records a not-covered row through "
    "_record_grading_failure instead of `return None`; a raise in coverage_rows_at_head fails "
    "closed."
)
CLAIM_66 = (
    "CONFIRM. Excerpt line 1255 logs `message=str(exc)[:200]` at WARNING before "
    "`_record_grading_failure` applies `_redacted`. The failed fetch that 0.5 says can echo "
    "`https://x-access-token:ghs_…@github.com` lands in the CI logs in clear, so a forge token "
    "leaks to anyone who can read job logs. Log `_redacted(str(exc))[:200]` here too."
)
REASONING_66 = (
    "Line 13 now logs message=_redacted(str(exc))[:200], so an https URL carrying an access "
    "token is redacted before it reaches the log."
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _cited_source(lines: int) -> str:
    """Return the cited file padded to ``lines`` lines after the fixed code."""
    rows = [CITED_SOURCE]
    count = CITED_SOURCE.count("\n")
    while count < lines:
        rows.append(f"PAD_{count} = '{'p' * 60}'\n")
        count += 1
    return "".join(rows)


def _head_tree(root: Path, cited_lines: int) -> str:
    files = {
        CITED: _cited_source(cited_lines),
        "src/repoach/review/spec_gate.py": SPEC_GATE_SOURCE,
        **{f"src/repoach/users/user_{n:03}.py": USER_SOURCE.format(n=n) for n in range(80)},
    }
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(root, "init", "-q", "-b", "feat/plan-absent")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fix: fail closed and redact")
    return _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("finding_id", "line", "claim", "reasoning", "words"),
    [
        (41, 9, CLAIM_41, REASONING_41, ("except", "Exception", "return", "None")),
        (66, 13, CLAIM_66, REASONING_66, ("message", "https", "access", "token")),
    ],
    ids=["finding-41", "finding-66"],
)
@pytest.mark.parametrize("cited_lines", [0, 2576], ids=["short", "2576-lines"])
def test_measured_refutation_settles_the_finding_at_the_next_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cited_lines: int,
    finding_id: int,
    line: int,
    claim: str,
    reasoning: str,
    words: tuple[str, ...],
) -> None:
    monkeypatch.delenv("REPOACH_REFUTER_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("REPOACH_REFUTER_CONTEXT_CEILING", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    head_b = _head_tree(root, cited_lines)
    for word in words:
        hits = _git(root, "grep", "-n", "-F", "-w", word).splitlines()
        assert len(hits) > 50, word
    db = tmp_path / "review.db"
    init_findings_schema(db)
    row_id = record_finding(
        db,
        Finding(
            pr_number=PR,
            head_sha=HEAD_A,
            round=1,
            finder="sentinel",
            claim_type=ClaimType.SECURITY,
            severity=Severity.BLOCKING,
            file=CITED,
            line_start=line,
            line_end=line,
            claim=claim,
            evidence_pointer=f"{CITED}:{line}",
        ),
    )
    prompts: list[str] = []

    def _factory():
        def _judge(prompt: str) -> str:
            prompts.append(prompt)
            return "VERDICT: " + json.dumps({"verdict": "refuted", "reasoning": reasoning})

        return _judge

    report = reverify_older_findings(
        db,
        pr_number=PR,
        repo_root=root,
        head_sha=head_b,
        ci_green=True,
        config=GatesConfig.builtin_default(),
        max_judged=10,
        judge_factory=_factory,
    )

    assert len(prompts) == 1, f"finding {finding_id}: the judge was not called"
    assert report.counts() == {"resolved": 1, "still_open": 0, "needs_judgement": 0}
    assert report.reasons() == []
    for word in words:
        assert f"search `{word}`" not in prompts[0]
    row = next(f for f in fetch_findings(db, PR) if f.id == row_id)
    assert row.status is FindingStatus.RESOLVED_AT_HEAD
    assert row.verification_result.startswith(f"resolved at {head_b[:7]}: ")
    if cited_lines:
        assert row.budget_used > REFUTER_CONTEXT_TOKENS_FLOOR
        assert "def _readable_base(base):" in prompts[0]
    else:
        assert row.budget_used == REFUTER_CONTEXT_TOKENS_FLOOR
