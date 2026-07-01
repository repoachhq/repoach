"""Unit tests for SP-FINDINGS-SENTINEL-REHOME.

The evidence-first re-homing of the legacy pre-verify writer: when the
initial verify/refute pass disproves a reviewer-origin finding (stored
status ``refuted``), the findings path must post the
``"Verified — challenge with evidence"`` sentinel on the originating
inline thread so the next review run's
:func:`fetch_resolved_disagreements` feeds it back into the reviewer
prompt (the anti-convergent-hallucination guard the legacy Coder
carried).

Pins: the reply body embeds the sentinel verbatim; threads are
re-located by (path, line) with a similarity tiebreak; CI-origin
findings and unmatched threads are skipped; posting is idempotent;
gh failures are swallowed; and the posted sentinel round-trips back
through the reader.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ferova.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    init_findings_schema,
    record_finding,
)
from ferova.review.reviewer import BotRole
from ferova.review.thread_context import (
    EVIDENCE_REPLY_SENTINEL,
    _format_finding_evidence_reply,
    _match_finding_thread,
    _thread_has_sentinel,
    fetch_resolved_disagreements,
    post_refuted_finding_sentinels,
)


def _finding(
    *,
    finder: str = "architect",
    file: str = "src/m.py",
    line: int = 12,
    claim: str = "add a test for compute()",
    claim_type: ClaimType = ClaimType.MISSING_TEST,
    status: FindingStatus = FindingStatus.REFUTED,
    method: str = "symbol_search",
    result: str = "compute exists in tests/src",
) -> Finding:
    return Finding(
        pr_number=1,
        head_sha="head123",
        round=1,
        finder=finder,
        claim_type=claim_type,
        severity=Severity.BLOCKING,
        file=file,
        line_start=line,
        line_end=line,
        claim=claim,
        evidence_pointer=f"{file}:{line}",
        status=status,
        verification_method=method,
        verification_result=result,
    )


class _FakeGh:
    """Minimal GhCli stand-in recording replies and modelling GitHub.

    ``reply_to_review_comment`` appends the new reply to the in-memory
    thread list so a later ``list_review_comments`` sees it — letting a
    single fake drive the writer-then-reader round-trip.
    """

    def __init__(self, threads: list[dict[str, object]], *, fail: bool = False) -> None:
        self._threads = list(threads)
        self.replies: list[tuple[int, str]] = []
        self.list_calls = 0
        self._fail = fail
        self._next_id = 10_000

    def list_review_comments(self, pr_number: int) -> list[dict[str, object]]:
        self.list_calls += 1
        return list(self._threads)

    def reply_to_review_comment(self, pr_number: int, *, comment_id: int, body: str):
        if self._fail:
            raise RuntimeError("gh down")
        self.replies.append((comment_id, body))
        self._next_id += 1
        self._threads.append(
            {
                "id": self._next_id,
                "in_reply_to_id": comment_id,
                "path": "",
                "line": None,
                "body": body,
            }
        )
        return SimpleNamespace(ok=True)


def _root(
    comment_id: int, *, path: str = "src/m.py", line: int = 12, body: str
) -> dict[str, object]:
    return {"id": comment_id, "in_reply_to_id": None, "path": path, "line": line, "body": body}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "findings.db"
    init_findings_schema(p)
    return p


def test_format_reply_embeds_sentinel_reason_and_method() -> None:
    body = _format_finding_evidence_reply(_finding())
    assert EVIDENCE_REPLY_SENTINEL in body
    assert body.startswith(f"**{EVIDENCE_REPLY_SENTINEL}:**")
    assert "compute exists in tests/src" in body
    assert "symbol_search" in body
    assert "missing_test" in body


def test_format_reply_falls_back_when_method_and_result_empty() -> None:
    body = _format_finding_evidence_reply(_finding(method="", result=""))
    assert EVIDENCE_REPLY_SENTINEL in body
    assert "refuted at head" in body
    assert "`verifier`" in body


def test_match_thread_by_path_and_line() -> None:
    roots = [_root(100, body="**[architect/major]** add a test")]
    assert _match_finding_thread(roots, _finding())["id"] == 100


def test_match_thread_returns_none_when_no_line_match() -> None:
    roots = [_root(100, line=99, body="**[architect/major]** add a test")]
    assert _match_finding_thread(roots, _finding(line=12)) is None


def test_match_thread_similarity_breaks_ties() -> None:
    roots = [
        _root(100, body="totally unrelated remark about naming"),
        _root(200, body="add a test for compute() please"),
    ]
    assert _match_finding_thread(roots, _finding(claim="add a test for compute()"))["id"] == 200


def test_thread_has_sentinel() -> None:
    assert _thread_has_sentinel([{"body": f"**{EVIDENCE_REPLY_SENTINEL}:** nope"}]) is True
    assert _thread_has_sentinel([{"body": "ordinary reply"}]) is False
    assert _thread_has_sentinel([]) is False


def test_posts_sentinel_on_refuted_reviewer_finding(db: Path) -> None:
    record_finding(db, _finding())
    gh = _FakeGh([_root(100, body="**[architect/major]** add a test for compute()")])

    posted = post_refuted_finding_sentinels(gh, db, 1)

    assert posted == 1
    assert len(gh.replies) == 1
    comment_id, body = gh.replies[0]
    assert comment_id == 100
    assert EVIDENCE_REPLY_SENTINEL in body


def test_posting_is_idempotent(db: Path) -> None:
    record_finding(db, _finding())
    threads = [
        _root(100, body="**[architect/major]** add a test for compute()"),
        {
            "id": 101,
            "in_reply_to_id": 100,
            "path": "",
            "line": None,
            "body": f"**{EVIDENCE_REPLY_SENTINEL}:** already done",
        },
    ]
    gh = _FakeGh(threads)

    posted = post_refuted_finding_sentinels(gh, db, 1)

    assert posted == 0
    assert gh.replies == []


def test_ci_origin_findings_skipped_without_gh_call(db: Path) -> None:
    record_finding(
        db,
        _finding(finder="ci", file="(ci):Test suite", line=0, claim_type=ClaimType.BROKEN_BEHAVIOR),
    )
    gh = _FakeGh([_root(100, body="**[architect/major]** add a test")])

    posted = post_refuted_finding_sentinels(gh, db, 1)

    assert posted == 0
    assert gh.list_calls == 0


def test_no_refuted_findings_never_calls_gh(db: Path) -> None:
    record_finding(db, _finding(status=FindingStatus.VERIFIED))
    gh = _FakeGh([_root(100, body="**[architect/major]** add a test")])

    posted = post_refuted_finding_sentinels(gh, db, 1)

    assert posted == 0
    assert gh.list_calls == 0


def test_unmatched_thread_is_skipped(db: Path) -> None:
    record_finding(db, _finding(file="src/other.py"))
    gh = _FakeGh([_root(100, body="**[architect/major]** add a test")])

    posted = post_refuted_finding_sentinels(gh, db, 1)

    assert posted == 0
    assert gh.replies == []


def test_gh_failure_is_swallowed(db: Path) -> None:
    record_finding(db, _finding())
    gh = _FakeGh([_root(100, body="**[architect/major]** add a test for compute()")], fail=True)

    posted = post_refuted_finding_sentinels(gh, db, 1)

    assert posted == 0


def test_posted_sentinel_round_trips_through_reader(db: Path) -> None:
    record_finding(db, _finding())
    gh = _FakeGh([_root(100, body="**[architect/major]** add a test for compute()")])

    assert post_refuted_finding_sentinels(gh, db, 1) == 1

    resolved = fetch_resolved_disagreements(gh, 1, role=BotRole.ARCHITECT)
    assert len(resolved) == 1
    assert resolved[0].file == "src/m.py"
    assert resolved[0].line == 12
    assert EVIDENCE_REPLY_SENTINEL in resolved[0].coder_evidence
