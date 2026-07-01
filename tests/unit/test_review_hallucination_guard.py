"""Tests for the runner-side hallucination guard (SP-DEV-V2-B3).

The guard post-processes a :class:`ReviewerOutcome` to:

* Downgrade ``blocker`` / ``major`` comments whose "missing X" claim
  is disproved by reading the cited file.
* Downgrade self-referential ``prompts/review/*`` comments that claim
  the prompt "includes the full plan" (when the diff just adds the
  ``{SPEC_PLAN}`` placeholder).
* Soften ``REQUEST_CHANGES`` to ``COMMENT`` when ≥50 % of the
  blocker+major evidence collapses.
"""

from __future__ import annotations

from pathlib import Path

from ferova.review.hallucination_guard import (
    apply_hallucination_guard,
    make_repo_file_reader,
    make_repo_symbol_searcher,
)
from ferova.review.reviewer import (
    BotRole,
    ReviewComment,
    ReviewerOutcome,
    ReviewVerdict,
)


def _outcome(
    *,
    verdict: ReviewVerdict,
    comments: list[ReviewComment],
    role: BotRole = BotRole.SCRIBE,
) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=verdict,
        summary="ok",
        comments=comments,
        model_used="qwen/qwen3-next-80b-a3b-instruct",
        elapsed_s=0.1,
        tokens_used=64,
        raw_response="{}",
    )


def _reader(mapping: dict[str, str]):
    def _read(path: str) -> str | None:
        return mapping.get(path)

    return _read


def test_missing_x_claim_downgraded_when_token_in_file():
    comment = ReviewComment(
        file="src/ferova/foo.py",
        line=42,
        severity="major",
        body="Args: section header is missing on `respond` — please add a Google-style docstring.",
    )
    out = _outcome(verdict=ReviewVerdict.REQUEST_CHANGES, comments=[comment])
    reader = _reader(
        {
            "src/ferova/foo.py": (
                'def respond(x):\n    """Do a thing.\n\n    Args:\n        x: input.\n    """\n    return x\n'
            )
        }
    )

    guarded, events = apply_hallucination_guard(out, file_reader=reader)

    assert guarded.comments[0].severity == "nit"
    assert guarded.comments[0].body.startswith("[guard:downgraded]")
    assert guarded.verdict == ReviewVerdict.COMMENT
    assert len(events) == 1
    assert events[0].reason == "missing_token_found_in_file"
    assert "Args" in events[0].tokens_found
    assert events[0].original_severity == "major"


def test_missing_x_claim_preserved_when_token_truly_absent():
    comment = ReviewComment(
        file="src/ferova/foo.py",
        line=10,
        severity="major",
        body="Returns section is missing on `respond`.",
    )
    out = _outcome(verdict=ReviewVerdict.REQUEST_CHANGES, comments=[comment])
    reader = _reader(
        {
            "src/ferova/foo.py": "def respond(x):\n    return x\n",
        }
    )

    guarded, events = apply_hallucination_guard(out, file_reader=reader)

    assert guarded.comments[0].severity == "major"
    assert guarded.verdict == ReviewVerdict.REQUEST_CHANGES
    assert events == []


def test_self_referential_prompt_template_downgraded():
    comment = ReviewComment(
        file="prompts/review/tester_0.1.0.md",
        line=80,
        severity="blocker",
        body="The prompt incorrectly includes the full plan instead of just a placeholder.",
    )
    out = _outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[comment],
        role=BotRole.ARCHITECT,
    )

    guarded, events = apply_hallucination_guard(out, file_reader=lambda _p: None)

    assert guarded.comments[0].severity == "nit"
    assert "self_referential" in guarded.comments[0].body
    assert guarded.verdict == ReviewVerdict.COMMENT
    assert len(events) == 1
    assert events[0].reason == "self_referential"


def test_domain_vocab_french_rename_downgraded():
    comment = ReviewComment(
        file="prompts/review/architect_0.1.0.md",
        line=77,
        severity="major",
        body="Remove the French wording 'Spec' — code should be English-only per CLAUDE.md.",
    )
    out = _outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[comment],
        role=BotRole.ARCHITECT,
    )

    guarded, events = apply_hallucination_guard(out, file_reader=lambda _p: None)

    assert guarded.comments[0].severity == "nit"
    assert guarded.verdict == ReviewVerdict.COMMENT
    assert len(events) == 1


def test_unreadable_file_leaves_comment_untouched():
    comment = ReviewComment(
        file="src/ferova/foo.py",
        line=1,
        severity="major",
        body="Args: section is missing.",
    )
    out = _outcome(verdict=ReviewVerdict.REQUEST_CHANGES, comments=[comment])

    guarded, events = apply_hallucination_guard(out, file_reader=lambda _p: None)

    assert guarded.comments[0].severity == "major"
    assert guarded.verdict == ReviewVerdict.REQUEST_CHANGES
    assert events == []


def test_partial_downgrade_keeps_request_changes():
    real_blocker = ReviewComment(
        file="src/ferova/foo.py",
        line=20,
        severity="blocker",
        body="Hard-coded credential in source — security risk.",
    )
    halluc_blocker = ReviewComment(
        file="src/ferova/foo.py",
        line=42,
        severity="major",
        body="Args: section header is missing.",
    )
    out = _outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[real_blocker, halluc_blocker],
    )
    reader = _reader(
        {
            "src/ferova/foo.py": (
                'def respond(x):\n    """Do a thing.\n\n    Args:\n        x: input.\n    """\n    return x\n'
            )
        }
    )

    guarded, events = apply_hallucination_guard(out, file_reader=reader)

    severities = sorted(c.severity for c in guarded.comments)
    assert severities == ["blocker", "nit"]
    assert guarded.verdict == ReviewVerdict.REQUEST_CHANGES
    assert len(events) == 1
    assert events[0].reason == "missing_token_found_in_file"


def test_no_blocker_or_major_returns_outcome_unchanged():
    nit = ReviewComment(
        file="src/ferova/foo.py",
        line=1,
        severity="nit",
        body="Returns section is missing.",
    )
    out = _outcome(verdict=ReviewVerdict.COMMENT, comments=[nit])

    guarded, events = apply_hallucination_guard(out, file_reader=lambda _p: None)

    assert guarded.comments[0].severity == "nit"
    assert guarded.verdict == ReviewVerdict.COMMENT
    assert events == []


def test_pr20_spec_embed_claim_downgraded_when_file_has_no_markers():
    """PR #20 live failure mode: Architect claimed reviewer.py embeds
    the full spec doc as literal text. Verification: the .py file
    has zero ``## Pourquoi`` / ``# SP-`` headers, so the claim is false.
    """
    comment = ReviewComment(
        file="src/ferova/review/reviewer.py",
        line=192,
        severity="blocker",
        body=(
            "The _render_prompt method embeds the full SP-DEV-V2-B spec "
            "doc as literal text in the prompt template substitution, not "
            "just the {SPEC_PLAN} placeholder."
        ),
    )
    out = _outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[comment],
        role=BotRole.ARCHITECT,
    )
    reader = _reader(
        {
            "src/ferova/review/reviewer.py": (
                "class Reviewer:\n    def _render_prompt(self, diff, *, spec_plan=None):\n"
                '        return template.replace("{SPEC_PLAN}", spec_plan or "")\n'
            )
        }
    )

    guarded, events = apply_hallucination_guard(out, file_reader=reader)

    assert guarded.comments[0].severity == "nit"
    assert guarded.verdict == ReviewVerdict.COMMENT
    assert len(events) == 1
    assert events[0].reason == "spec_embed_claim_unproven"


def test_spec_embed_claim_preserved_when_file_actually_embeds_doc():
    """Defensive: a file that DOES embed spec-doc markers leaves
    the claim untouched (Architect would be correctly calling out a
    real leak in that case).
    """
    comment = ReviewComment(
        file="src/ferova/review/leak.py",
        line=1,
        severity="blocker",
        body="This file embeds the full spec doc.",
    )
    out = _outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[comment],
        role=BotRole.ARCHITECT,
    )
    reader = _reader(
        {
            "src/ferova/review/leak.py": (
                '"""# SP-FAKE-EXAMPLE\n\n## Why\nleaked content here.\n"""\n'
            )
        }
    )

    guarded, events = apply_hallucination_guard(out, file_reader=reader)

    assert guarded.comments[0].severity == "blocker"
    assert guarded.verdict == ReviewVerdict.REQUEST_CHANGES
    assert events == []


def test_make_repo_file_reader_blocks_traversal(tmp_path: Path):
    inside = tmp_path / "src" / "foo.py"
    inside.parent.mkdir(parents=True)
    inside.write_text("hello\n")

    reader = make_repo_file_reader(tmp_path)

    assert reader("src/foo.py") == "hello\n"
    assert reader("../etc/passwd") is None
    assert reader("does/not/exist.py") is None


def _searcher(known: set[str]):
    def _search(symbol: str) -> bool:
        return symbol in known

    return _search


def test_missing_test_claim_downgraded_when_test_exists_on_disk():
    """SP-CODER-EVIDENCE-CHALLENGE — Tester hallucination caught by symbol search."""
    comment = ReviewComment(
        file="src/ferova/snapshots.py",
        line=87,
        severity="major",
        body=(
            "CONFIRM — performance_7d has no test. Add "
            "test_performance_7d_won_lost_calculates_roi using tmp_path."
        ),
    )
    out = _outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[comment],
        role=BotRole.TESTER,
    )

    guarded, events = apply_hallucination_guard(
        out,
        file_reader=lambda _p: None,
        symbol_searcher=_searcher({"test_performance_7d_won_lost_calculates_roi"}),
    )

    assert guarded.comments[0].severity == "nit"
    assert "missing_test_found" in guarded.comments[0].body
    assert guarded.verdict == ReviewVerdict.COMMENT
    assert len(events) == 1
    assert events[0].reason == "missing_test_found_on_disk"
    assert "test_performance_7d_won_lost_calculates_roi" in events[0].tokens_found


def test_missing_test_claim_preserved_when_test_truly_absent():
    """Genuinely missing test → no downgrade, verdict stays REQUEST_CHANGES."""
    comment = ReviewComment(
        file="src/ferova/snapshots.py",
        line=87,
        severity="major",
        body="No test for performance_7d — add test_performance_7d_happy.",
    )
    out = _outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[comment],
        role=BotRole.TESTER,
    )

    guarded, events = apply_hallucination_guard(
        out,
        file_reader=lambda _p: None,
        symbol_searcher=_searcher(set()),
    )

    assert guarded.comments[0].severity == "major"
    assert guarded.verdict == ReviewVerdict.REQUEST_CHANGES
    assert events == []


def test_missing_test_check_skipped_when_no_searcher_provided():
    """Backwards-compat — call sites without the optional searcher still work."""
    comment = ReviewComment(
        file="src/ferova/snapshots.py",
        line=87,
        severity="major",
        body="No test for performance_7d — add test_performance_7d_happy.",
    )
    out = _outcome(
        verdict=ReviewVerdict.REQUEST_CHANGES,
        comments=[comment],
        role=BotRole.TESTER,
    )

    guarded, events = apply_hallucination_guard(out, file_reader=lambda _p: None)

    assert guarded.comments[0].severity == "major"
    assert events == []


def test_make_repo_symbol_searcher_finds_existing_test_function(tmp_path: Path):
    """The default searcher resolves ``def test_X`` from a real worktree."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_foo.py").write_text(
        "def test_real_one():\n    assert True\n",
        encoding="utf-8",
    )

    searcher = make_repo_symbol_searcher(tmp_path)

    assert searcher("test_real_one") is True
    assert searcher("test_phantom") is False
    assert searcher("") is False
