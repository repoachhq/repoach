"""End-to-end integration test for SP-FINDINGS-INIT-RACE (AC4).

Drives a real :meth:`ReviewTeamOrchestrator.review_pr` run — the actual
four-worker ``ThreadPoolExecutor`` fan-out across real ``Architect`` /
``Sentinel`` / ``Tester`` / ``Scribe`` instances — against a fresh,
never-initialized ``db_path``. Each reviewer has ``db_path`` set, so
``Reviewer.review_diff`` really calls ``render_lens_track_record`` ->
``fetch_all_findings`` -> ``init_findings_schema`` from four concurrent
threads, reproducing the exact call path that raced
``sqlite3.OperationalError: table pr_findings already exists`` in
production the first time a review ran against a brand-new findings
ledger.

Only the network/subprocess boundary is faked: ``GhCli`` is replaced by
a truthful stand-in returning a canned diff (mirroring the existing
``_StubGhCli`` pattern in ``tests/unit/test_review_team.py``),
``Reviewer._call_with_retry`` — the LLM call boundary — returns a
canned ``APPROVE`` outcome instantly, and ``recall_review_lessons`` —
the ``agentmemory`` HTTP boundary — returns ``[]``. Every other code
path (schema init, findings/review persistence, hallucination guard,
mechanical verification, judging) runs for real.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import structlog
from sqlalchemy import create_engine, inspect, text
from structlog.testing import capture_logs

import repoach.review.review_lessons as review_lessons
from repoach.review.gh_client import GhResult
from repoach.review.orchestrator import ReviewTeamOrchestrator
from repoach.review.reviewer import Reviewer, ReviewVerdict

_CANNED_DIFF = (
    "diff --git a/a.py b/a.py\n"
    "--- a/a.py\n"
    "+++ b/a.py\n"
    "@@ -1 +1 @@\n"
    "+x = 1\n"
    "diff --git a/src/x.py b/src/x.py\n"
    "--- a/src/x.py\n"
    "+++ b/src/x.py\n"
    "@@ -1 +1 @@\n"
    "+y = 2\n"
)

_LEDGER_TABLES = (
    "pr_findings",
    "pr_review_integrity",
    "pr_reviews",
    "pr_coder_responses",
    "pr_merges",
)


class _StubGhCli:
    """Truthful GhCli stand-in returning a canned diff, no network.

    Mirrors the established ``_StubGhCli`` pattern in
    ``tests/unit/test_review_team.py``; only the two methods the
    ``post_to_github=False`` code path actually reaches are needed.
    """

    def pr_diff(self, pr_number: int) -> GhResult:
        return GhResult(0, _CANNED_DIFF, "", argv=["gh", "pr", "diff"])

    def list_review_comments(self, pr_number: int) -> list[dict[str, object]]:
        return []


def _canned_call_with_retry(
    self: Reviewer, prompt: str, *, pr_number: int | None
) -> tuple[ReviewVerdict, str, list, SimpleNamespace]:
    """Stand-in for the LLM call boundary: instant canned APPROVE, no comments."""
    del prompt, pr_number
    result = SimpleNamespace(model_used="test-model", elapsed_s=0.0, tokens_used=0, text="{}")
    return ReviewVerdict.APPROVE, "canned approve, nothing to flag", [], result


def test_review_pr_four_reviewer_threads_do_not_race_pr_findings_creation(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Four real reviewer threads racing a fresh db_path never emit the db_error.

    Regression coverage for SP-FINDINGS-INIT-RACE at the level the
    incident actually fired: through the orchestrator's real
    four-worker fan-out rather than a synthetic thread harness.
    """
    monkeypatch.setattr(review_lessons, "_log", structlog.get_logger("test.review_lessons"))
    monkeypatch.setattr(Reviewer, "_call_with_retry", _canned_call_with_retry)
    monkeypatch.setattr("repoach.review.orchestrator.recall_review_lessons", lambda _query: [])

    db_path = tmp_path / "review.db"
    assert not db_path.exists()

    orch = ReviewTeamOrchestrator(
        gh=_StubGhCli(),
        db_path=db_path,
        post_to_github=False,
        max_workers=4,
        repo_root=tmp_path,
    )

    with capture_logs() as logs:
        team = orch.review_pr(pr_number=101)

    assert team.pr_number == 101

    db_error_events = [entry for entry in logs if entry["event"] == "review.track_record.db_error"]
    assert db_error_events == [], f"unexpected track-record db errors: {db_error_events}"

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    for table in _LEDGER_TABLES:
        assert inspector.has_table(table), f"{table} missing after review_pr"

    with engine.connect() as conn:
        for table in _LEDGER_TABLES:
            conn.execute(text(f"SELECT * FROM {table}"))
