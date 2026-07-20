"""Smoke tests pinning the SP-SILENT-EXCEPT-REVIEW-LAYER contract.

Each of the 39 silent ``except`` handlers in
``src/repoach/review/`` now emits a structured log before
swallowing. Same template as
``tests/unit/test_orchestrator_silent_except_logging.py``: anchor
guards (lint baseline + zero ``review/`` residual) plus
source-pinned checks per file.

The review layer is the bot pipeline itself (Architect, Sentinel,
Tester, Scribe, Coder, plus the orchestrator and GitHub client).
Many sites are tolerant-parse fallbacks (DEBUG); a few are real
I/O failures (WARNING). The pinned events let us catch a rename
or accidental removal of any log call without depending on
brittle integration scaffolding.
"""

from __future__ import annotations

from pathlib import Path

_REVIEW_DIR = Path(__file__).resolve().parents[2] / "src" / "repoach" / "review"


def _read(name: str) -> str:
    return (_REVIEW_DIR / name).read_text(encoding="utf-8")


class TestLintBaselineRatchet:
    """``MAX_SILENT_EXCEPT`` is at-or-below the 87 baseline set by this PR.

    Relaxed to ``<=`` (rather than ``==``) so future ratchet PRs don't
    have to come back and bump the literal here.
    """

    def test_baseline_at_or_below_87(self) -> None:
        from repoach.lint.no_silent_except import MAX_SILENT_EXCEPT

        assert MAX_SILENT_EXCEPT <= 87


class TestReviewLayerHasZeroResidual:
    """``review/`` contributes 0 silent-except violations after this PR."""

    def test_zero_review_residual(self) -> None:
        from repoach.lint.no_silent_except import scan

        repo_root = Path(__file__).resolve().parents[2]
        violations = scan([repo_root / "src" / "repoach" / "review"])
        assert violations == [], (
            f"review/ should have 0 silent-excepts; got {[v.format() for v in violations]}"
        )


class TestEventNamesPinnedInSource:
    """Each new event name appears as a literal string in its file."""

    def test_auto_merge_event_name_present(self) -> None:
        src = _read("auto_merge.py")
        assert '"auto_merge.persist_failed"' in src

    def test_dev_runner_event_name_present(self) -> None:
        src = _read("dev_runner.py")
        assert '"dev_runner.path_outside_root"' in src

    def test_orchestrator_event_names_present(self) -> None:
        src = _read("orchestrator.py")
        assert '"orchestrator.comment_id_json_decode_failed"' in src
        assert '"orchestrator.comment_id_int_cast_failed"' in src

    def test_hallucination_guard_event_names_present(self) -> None:
        src = _read("hallucination_guard.py")
        assert '"hallucination_guard.file_read_failed"' in src
        assert '"hallucination_guard.file_scan_skipped"' in src

    def test_thread_context_event_names_present(self) -> None:
        src = _read("thread_context.py")
        assert '"thread_context.unknown_role_tag"' in src
        assert '"thread_context.bad_in_reply_id"' in src
        assert '"thread_context.bad_root_id"' in src

    def test_gh_client_event_names_present(self) -> None:
        src = _read("gh_client.py")
        assert '"gh_client.pr_view_decode_failed"' in src
        assert '"gh_client.review_comments_decode_failed"' in src
        assert '"gh_client.pr_head_sha_decode_failed"' in src
        assert '"gh_client.find_archive_decode_failed"' in src
        assert '"gh_client.archive_body_decode_failed"' in src
        assert '"gh_client.list_open_prs_decode_failed"' in src

    def test_coder_loop_event_names_present(self) -> None:
        src = _read("coder_loop.py")
        assert '"coder_loop.size_guard_unreadable"' in src
        assert '"coder_loop.placeholder_log_mkdir_failed"' in src
        assert '"coder_loop.placeholder_log_write_failed"' in src

    def test_reviewer_event_names_present(self) -> None:
        src = _read("reviewer.py")
        assert '"reviewer.excerpt_path_outside_root"' in src
        assert '"reviewer.excerpt_read_failed"' in src
        assert '"reviewer.verdict_blob_unparseable"' in src
        assert '"reviewer.bad_comment_line"' in src
        assert '"reviewer.parse_log_mkdir_failed"' in src
        assert '"reviewer.parse_log_write_failed"' in src
        assert '"reviewer.coder_plan_raw_decode_failed"' in src
        assert '"reviewer.coder_plan_fence_decode_failed"' in src
        assert '"reviewer.coder_plan_blob_decode_failed"' in src
        assert '"reviewer.coder_plan_salvage_failed"' in src
        assert '"reviewer.fix_detector_blob_unparseable"' in src
