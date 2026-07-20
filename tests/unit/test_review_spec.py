"""Tests for the spec loader and branch-name detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from repoach.review.spec import (
    SPECS_DIR,
    SpecPlan,
    detect_spec_from_branch,
    load_spec,
    maybe_load_active_spec,
)

# ---------------------------------------------------------------------------
# detect_spec_from_branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "branch, expected",
    [
        ("feat/sp-sec-hardening", "SP-SEC"),
        ("feat/sp-dev-runner", "SP-DEV"),
        ("feat/SP-PB1-registry", "SP-PB1"),
        ("fix/sp-tm-tennis-madrid", "SP-TM"),
        ("chore/sp-archv2-doc", "SP-ARCHV2"),
        ("feat/sp-pb1-impl-something", "SP-PB1"),
    ],
)
def test_detect_spec_from_branch_matches_convention(branch: str, expected: str) -> None:
    assert detect_spec_from_branch(branch) == expected


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "develop",
        "feat/something-else",
        "feat/review-team-nim",
        "",
        "feat/",
    ],
)
def test_detect_spec_from_branch_returns_none_when_no_match(branch: str) -> None:
    assert detect_spec_from_branch(branch) is None


def test_detect_spec_greedy_matches_longest_known_id(tmp_path: Path) -> None:
    """When several specs share a prefix (SP-WA-AGENT vs
    SP-WA-AGENT-A2), the longest match wins.

    Regression guard for the multi-scope hallucination observed on
    PR #31 (2026-04-30): the legacy regex captured ``SP-WA`` for
    ``feat/sp-wa-agent-a2-drop-classifier``, which made the
    plan-aware reviewers load the wrong spec section.
    """
    base = tmp_path / "docs" / "specs"
    base.mkdir(parents=True)
    # Drop two competing specs on disk.
    (base / "2026-04-30_SP-WA-AGENT_unified.md").write_text(
        "# SP-WA-AGENT umbrella\n\nshort body\n", encoding="utf-8"
    )
    (base / "2026-04-30_SP-WA-AGENT-A2_drop-classifier.md").write_text(
        "# SP-WA-AGENT-A2\n\nA2 sub-scope body\n", encoding="utf-8"
    )

    assert (
        detect_spec_from_branch("feat/sp-wa-agent-a2-drop-classifier", root=tmp_path)
        == "SP-WA-AGENT-A2"
    )
    # The umbrella id still resolves on its own branch.
    assert detect_spec_from_branch("feat/sp-wa-agent-overview", root=tmp_path) == "SP-WA-AGENT"


def test_detect_spec_handles_multi_segment_ids(tmp_path: Path) -> None:
    """Multi-segment IDs like ``SP-DEV-V2-B2`` resolve correctly
    when the spec exists on disk.
    """
    base = tmp_path / "docs" / "specs"
    base.mkdir(parents=True)
    (base / "2026-04-30_SP-DEV-V2-B2_plan_aware.md").write_text("body", encoding="utf-8")
    (base / "2026-04-30_SP-DEV-V2-B_coder.md").write_text("body", encoding="utf-8")
    (base / "2026-04-30_SP-DEV-V2-A_syntax.md").write_text("body", encoding="utf-8")

    assert detect_spec_from_branch("feat/sp-dev-v2-b2-impl", root=tmp_path) == "SP-DEV-V2-B2"
    # B without the "2" picks the right one (longest-prefix).
    assert detect_spec_from_branch("feat/sp-dev-v2-b-impl", root=tmp_path) == "SP-DEV-V2-B"
    assert detect_spec_from_branch("feat/sp-dev-v2-a-impl", root=tmp_path) == "SP-DEV-V2-A"


def test_detect_spec_falls_back_to_legacy_regex_when_no_spec(
    tmp_path: Path,
) -> None:
    """When ``docs/specs/`` is empty or missing, the legacy
    short-form regex still resolves common branch shapes — preserves
    the behaviour for environments without a spec directory.
    """
    # Empty docs/specs
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    assert detect_spec_from_branch("feat/sp-sec-hardening", root=tmp_path) == "SP-SEC"
    # No docs/specs at all.
    other = tmp_path / "no-docs"
    other.mkdir()
    assert detect_spec_from_branch("feat/sp-pb1-registry", root=other) == "SP-PB1"


# ---------------------------------------------------------------------------
# load_spec
# ---------------------------------------------------------------------------


def _seed_spec(tmp_path: Path, name: str, content: str) -> Path:
    """Create ``tmp_path/docs/specs/<name>.md`` and return it."""
    d = tmp_path / SPECS_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content, encoding="utf-8")
    return p


_SAMPLE_PLAN = """\
# SP-FOO — Sample feature

> One-line abstract describing what the spec does.

This is the first paragraph that becomes the summary.  Written in
plain English so we can humans triage at a glance.

## Plan

- Modify `src/repoach/foo.py` to add a method.
- Add `tests/unit/test_foo.py` with three tests.
- See also `docs/runbooks/foo_runbook.md` for ops notes.
"""


def test_load_spec_returns_plan_with_title_summary_and_paths(tmp_path: Path) -> None:
    _seed_spec(tmp_path, "2026-04-29_SP-FOO_sample.md", _SAMPLE_PLAN)
    plan = load_spec("FOO", root=tmp_path)
    assert plan.id == "SP-FOO"
    assert "SP-FOO" in plan.title
    assert "first paragraph" in plan.summary
    # Referenced paths picked up from backticks.
    assert "src/repoach/foo.py" in plan.referenced_paths
    assert "tests/unit/test_foo.py" in plan.referenced_paths
    assert "docs/runbooks/foo_runbook.md" in plan.referenced_paths
    # raw_markdown carries the full content.
    assert "first paragraph" in plan.raw_markdown


def test_load_spec_accepts_canonical_id(tmp_path: Path) -> None:
    _seed_spec(tmp_path, "2026-04-29_SP-FOO_sample.md", _SAMPLE_PLAN)
    plan = load_spec("SP-FOO", root=tmp_path)
    assert plan.id == "SP-FOO"


def test_load_spec_accepts_lowercase_id(tmp_path: Path) -> None:
    _seed_spec(tmp_path, "2026-04-29_SP-FOO_sample.md", _SAMPLE_PLAN)
    plan = load_spec("sp-foo", root=tmp_path)
    assert plan.id == "SP-FOO"


def test_load_spec_picks_most_recent_when_multiple_match(tmp_path: Path) -> None:
    _seed_spec(tmp_path, "2026-01-01_SP-FOO_v1.md", "# SP-FOO\n\nold version")
    _seed_spec(tmp_path, "2026-04-29_SP-FOO_v2.md", "# SP-FOO\n\nnew version")
    plan = load_spec("FOO", root=tmp_path)
    # Lex-sorted glob → most recent wins (last entry).
    assert "new version" in plan.raw_markdown
    assert "v2" in plan.file_path.name


def test_load_spec_raises_when_id_unknown(tmp_path: Path) -> None:
    (tmp_path / SPECS_DIR).mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileNotFoundError, match="SP-NOPE"):
        load_spec("NOPE", root=tmp_path)


def test_load_spec_caps_huge_plans(tmp_path: Path) -> None:
    """Plans larger than the prompt-budget cap are truncated with a marker."""
    huge = "# SP-BIG\n\n" + ("filler line\n" * 5000)
    _seed_spec(tmp_path, "2026-04-29_SP-BIG_huge.md", huge)
    plan = load_spec("BIG", root=tmp_path)
    assert "[... plan truncated" in plan.raw_markdown
    assert len(plan.raw_markdown) < len(huge)


# ---------------------------------------------------------------------------
# maybe_load_active_spec
# ---------------------------------------------------------------------------


def test_maybe_load_uses_explicit_id_first(tmp_path: Path) -> None:
    _seed_spec(tmp_path, "2026-04-29_SP-FOO_sample.md", _SAMPLE_PLAN)
    plan = maybe_load_active_spec(explicit_id="FOO", branch="feat/sp-bar-other", root=tmp_path)
    assert plan is not None
    assert plan.id == "SP-FOO"


def test_maybe_load_falls_back_to_branch(tmp_path: Path) -> None:
    _seed_spec(tmp_path, "2026-04-29_SP-FOO_sample.md", _SAMPLE_PLAN)
    plan = maybe_load_active_spec(branch="feat/sp-foo-runner", root=tmp_path)
    assert plan is not None
    assert plan.id == "SP-FOO"


def test_maybe_load_returns_none_when_no_match(tmp_path: Path) -> None:
    (tmp_path / SPECS_DIR).mkdir(parents=True, exist_ok=True)
    plan = maybe_load_active_spec(branch="develop", root=tmp_path)
    assert plan is None


def test_maybe_load_returns_none_when_doc_missing(tmp_path: Path) -> None:
    (tmp_path / SPECS_DIR).mkdir(parents=True, exist_ok=True)
    plan = maybe_load_active_spec(explicit_id="MISSING", root=tmp_path)
    assert plan is None


def test_maybe_load_handles_empty_inputs(tmp_path: Path) -> None:
    plan = maybe_load_active_spec(root=tmp_path)
    assert plan is None
    plan = maybe_load_active_spec(explicit_id="", branch="", root=tmp_path)
    assert plan is None


# ---------------------------------------------------------------------------
# SpecPlan dataclass
# ---------------------------------------------------------------------------


def test_spec_plan_is_frozen() -> None:
    plan = SpecPlan(
        id="SP-X",
        file_path=Path("/tmp/x.md"),
        raw_markdown="# X",
        title="X",
        summary="x.",
    )
    with pytest.raises((AttributeError, Exception)):
        plan.id = "SP-Y"


# ---------------------------------------------------------------------------
# Edge cases flagged by the Tester NIM on PR #6 (SP-DEV bootstrap review)
# ---------------------------------------------------------------------------


def test_load_spec_handles_empty_body_after_title(tmp_path: Path) -> None:
    """Plan with just a title and no body → summary='' and no referenced paths."""
    _seed_spec(tmp_path, "2026-04-29_SP-EMPTY_t.md", "# SP-EMPTY\n")
    plan = load_spec("EMPTY", root=tmp_path)
    assert plan.title == "SP-EMPTY"
    assert plan.summary == ""
    assert plan.referenced_paths == ()


def test_load_spec_no_title_returns_empty_title(tmp_path: Path) -> None:
    """Plan without a leading H1 still loads, with empty title."""
    _seed_spec(tmp_path, "2026-04-29_SP-NOTITLE_t.md", "Body without title.")
    plan = load_spec("NOTITLE", root=tmp_path)
    assert plan.title == ""
    # First non-heading paragraph still surfaces as summary.
    assert "Body without title" in plan.summary


@pytest.mark.parametrize(
    "branch",
    [
        "/feat/sp-x-y",
        "feat/sp--double-dash",
        "feat/sp-",
    ],
)
def test_detect_spec_from_branch_handles_malformed(branch: str) -> None:
    """Malformed branch names yield None instead of garbage ids."""
    out = detect_spec_from_branch(branch)
    assert out is None or out.startswith("SP-")


def test_scan_referenced_paths_ignores_non_repo_paths(tmp_path: Path) -> None:
    """Backtick paths outside the conventional dirs are not extracted."""
    plan_md = (
        "# SP-X\n\n"
        "See `/etc/passwd` and `~/.ssh/id_rsa` and `random/path/foo.py`\n"
        "but DO read `src/repoach/foo.py`.\n"
    )
    _seed_spec(tmp_path, "2026-04-29_SP-X_t.md", plan_md)
    plan = load_spec("X", root=tmp_path)
    assert "src/repoach/foo.py" in plan.referenced_paths
    assert "/etc/passwd" not in plan.referenced_paths
    assert "random/path/foo.py" not in plan.referenced_paths
