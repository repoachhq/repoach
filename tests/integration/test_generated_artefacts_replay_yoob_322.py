"""SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ AC11: Yoob !322 replayed with the declaration.

The fixture under ``tests/fixtures/generated_artefacts/yoob_322/`` holds
!322's diff shape as GitLab served it on 2026-10-27: the corpus unit and
the user-guide unit verbatim, the embeddings unit with its two
1 556 480-character ``vectors`` lines replaced by a placeholder this test
expands back to that length (a 3.1 MB unit, 22 times the reviewers'
cap), the two ledger rows that held the MR (findings 3 and 6, oversized
``spec_gap`` raised at ``0cc9008`` and ``7cd8971``, deferred
``needs judgement`` at ``619a413``), the recorded ``--check`` output
(exit 0), and the base ``repoach.toml`` declaring the embeddings.

The next review, with the declaration at the base and the recorded check
replayed by the verification job, approves, and findings 3 and 6 end
``resolved_at_head`` without a refuter call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from repoach.review.generated_artefacts import RunResult
from repoach.review.reviewer import _DIFF_HARD_CAP_CHARS
from tests.fixtures.generated_artefacts.harness import (
    RecordingRunner,
    Repo,
    install,
    review,
    verify,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "generated_artefacts" / "yoob_322"
SHAPE = json.loads((FIXTURE / "shape.json").read_text(encoding="utf-8"))
EMBEDDINGS = SHAPE["embeddings"]["path"]
CHECK_LINE = "scripts/index_knowledge_corpus.py --repo-root . --check"
PR = SHAPE["mr"]


def _sized(fill: str, size: int) -> str:
    """A JSON-shaped artefact of exactly ``size`` bytes."""
    head = '{\n "dtype": "float32-le",\n "vectors": "'
    tail = '"\n}\n'
    return head + fill * (size - len(head) - len(tail)) + tail


def _diff() -> str:
    """!322's three units, in GitLab's order, the embeddings expanded to its real size."""
    width = SHAPE["embeddings"]["vectors_line_chars"]
    template = (FIXTURE / SHAPE["embeddings"]["template"]).read_text(encoding="utf-8")
    first = template.index(SHAPE["embeddings"]["placeholder"])
    embeddings = (
        template[:first]
        + "a" * width
        + template[first + len(SHAPE["embeddings"]["placeholder"]) :].replace(
            SHAPE["embeddings"]["placeholder"], "b" * width
        )
    )
    corpus = (FIXTURE / "corpus.diff").read_text(encoding="utf-8")
    guide = (FIXTURE / "user_guide.diff").read_text(encoding="utf-8")
    return corpus + embeddings + guide


def _ledger(db_path: Path) -> list[str]:
    """Record findings 3 and 6 as the ledger held them; return their heads."""
    init_findings_schema(db_path)
    rows = json.loads((FIXTURE / "ledger.json").read_text(encoding="utf-8"))["findings"]
    for row in rows:
        record_finding(
            db_path,
            Finding(
                pr_number=PR,
                head_sha=row["head_sha"],
                round=row["round"],
                finder=row["finder"],
                claim_type=ClaimType(row["claim_type"]),
                severity=Severity(row["severity"]),
                file=row["file"],
                line_start=0,
                line_end=0,
                claim=row["claim"],
                evidence_pointer=row["evidence_pointer"],
                status=FindingStatus(row["status"]),
                verification_method=row["verification_method"],
                verification_result=row["verification_result"],
                checked_at_sha=row["checked_at_sha"],
            ),
        )
    return [row["head_sha"] for row in rows]


def test_yoob_322_approves_once_the_embeddings_are_declared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The declared, checked embeddings no longer hold !322: APPROVE, findings 3 and 6 settled."""
    bench = install(monkeypatch, tmp_path)
    size = SHAPE["embeddings"]["bytes_before"]
    repo = Repo(
        tmp_path / "yoob",
        {
            "repoach.toml": (FIXTURE / "repoach.toml").read_text(encoding="utf-8"),
            "scripts/index_knowledge_corpus.py": '"""Recorded by the fixture."""\n',
            "api/data/knowledge_corpus.json": '{"chunks": 380}\n',
            "docs/user_guide/ACCES_WEB_DISTANT_STAGIAIRE.md": "studio.yoob.dev\n",
            EMBEDDINGS: _sized("a", size),
        },
    )
    head = repo.push_head(
        {
            "api/data/knowledge_corpus.json": '{"chunks": 380, "host": "yoob.dev"}\n',
            "docs/user_guide/ACCES_WEB_DISTANT_STAGIAIRE.md": "yoob.dev\n",
            EMBEDDINGS: _sized("b", SHAPE["embeddings"]["bytes_after"]),
        },
        pr=PR,
    )
    repo.diffs[PR] = _diff()
    assert len(repo.diffs[PR]) > 22 * _DIFF_HARD_CAP_CHARS
    older = _ledger(bench.db_path)
    recorded = SHAPE["recorded_check"]
    runner = RecordingRunner(
        [
            RunResult(
                recorded["exit_code"],
                (FIXTURE / recorded["output"]).read_text(encoding="utf-8"),
            )
        ]
    )
    evidence = tmp_path / "review-generated.json"

    payload, verify_err, runner = verify(
        repo, monkeypatch, capsys, head=head, out=evidence, pr=PR, runner=runner
    )
    result = review(repo, monkeypatch, capsys, head=head, pr=PR, evidence=evidence)

    assert [call.argv for call in runner.calls] == [["python3", *recorded["argv"]]]
    assert payload["checks"][0]["state"] == "generated_artefact_verified"
    assert payload["checks"][0]["sources_changed"] == [
        "api/data/knowledge_corpus.json",
        "docs/user_guide/ACCES_WEB_DISTANT_STAGIAIRE.md",
    ]
    for diff in bench.prompts.diffs:
        assert "a" * 10_000 not in diff
        assert "b" * 10_000 not in diff
        assert len(diff) < _DIFF_HARD_CAP_CHARS
        assert f"generated artefact {EMBEDDINGS}: {size} -> {size} bytes" in diff
    line = re.compile(
        rf"^generated: {re.escape(EMBEDDINGS)} verified by {re.escape(CHECK_LINE)} \(\d+ ms\)$"
    )
    assert any(line.match(entry) for entry in result.stderr)
    assert any(line.match(entry) for entry in verify_err)
    assert result.payload["final_verdict"] == "APPROVE"
    settled = {f.head_sha: f for f in fetch_findings(bench.db_path, PR) if f.head_sha in older}
    assert set(settled) == set(older)
    for finding in settled.values():
        assert finding.status is FindingStatus.RESOLVED_AT_HEAD
        assert f"declared generated at {head[:7]}, verified by {CHECK_LINE}" in (
            finding.verification_result
        )
    assert [f for f in fetch_findings(bench.db_path, PR) if f.head_sha == head] == []
    assert bench.judge.calls == 0
