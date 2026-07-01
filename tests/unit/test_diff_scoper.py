"""Unit tests for diff_scoper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ferova.review.diff_scoper import scope_diff, split_diff
from ferova.review.reviewer import _DIFF_HARD_CAP_CHARS, Architect, ReviewVerdict

FIXTURE_DIFF = """\npreamble: changes in this batch
diff --git a/src/alpha.py b/src/alpha.py
index 0000001..0000002 100644
--- a/src/alpha.py
+++ b/src/alpha.py
@@ -1,3 +1,4 @@
def alpha():
-    pass
+    return 1
+
diff --git a/src/beta.py b/src/beta.py
index 0000003..0000004 100644
--- a/src/beta.py
+++ b/src/beta.py
@@ -1,3 +1,4 @@
def beta():
-    pass
+    return 2
+
diff --git a/src/gamma.py b/src/gamma.py
index 0000005..0000006 100644
--- a/src/gamma.py
+++ b/src/gamma.py
@@ -1,3 +1,4 @@
def gamma():
-    pass
+    return 3
+"""


def test_split_diff_units() -> None:
    units = split_diff(FIXTURE_DIFF)
    assert len(units) == 3
    assert units[0].path == "src/alpha.py"
    assert units[1].path == "src/beta.py"
    assert units[2].path == "src/gamma.py"
    for unit in units:
        assert unit.chars == len(unit.text)


def test_split_diff_empty() -> None:
    assert split_diff("") == []


def test_split_diff_preamble_attached() -> None:
    units = split_diff(FIXTURE_DIFF)
    assert "preamble: changes in this batch" in units[0].text
    assert "preamble" not in units[1].text
    assert "preamble" not in units[2].text


def test_scope_diff_omits_whole_units() -> None:
    units = split_diff(FIXTURE_DIFF)
    cap_one = units[0].chars + 10
    result = scope_diff(FIXTURE_DIFF, cap_one)
    assert result.included == ["src/alpha.py"]
    assert set(result.omitted) == {"src/beta.py", "src/gamma.py"}
    assert units[1].text not in result.prompt_diff
    assert units[2].text not in result.prompt_diff
    assert "diff --git a/src/alpha.py" in result.prompt_diff


def test_scope_diff_oversized_unit_never_cut() -> None:
    units = split_diff(FIXTURE_DIFF)
    cap_tiny = min(u.chars for u in units) - 1
    result = scope_diff(FIXTURE_DIFF, cap_tiny)
    assert set(result.oversized) == {"src/alpha.py", "src/beta.py", "src/gamma.py"}
    assert set(result.omitted) == {"src/alpha.py", "src/beta.py", "src/gamma.py"}
    assert result.included == []
    assert "diff --git" not in result.prompt_diff


def test_scope_diff_passthrough_when_under_cap() -> None:
    units = split_diff(FIXTURE_DIFF)
    cap_all = sum(u.chars for u in units) + 100
    result = scope_diff(FIXTURE_DIFF, cap_all)
    assert result.prompt_diff == FIXTURE_DIFF
    assert result.omitted == []
    assert result.oversized == []


def test_scope_diff_announcement_lists_omitted() -> None:
    units = split_diff(FIXTURE_DIFF)
    cap_one = units[0].chars + 10
    result = scope_diff(FIXTURE_DIFF, cap_one)
    assert "src/beta.py" in result.prompt_diff
    assert "src/gamma.py" in result.prompt_diff


def _make_oversized_diff(cap: int) -> str:
    """Build a two-unit diff whose total exceeds *cap* but each unit fits alone."""
    # Each unit is slightly over cap/2 so: unit0 included, unit1 omitted.
    unit_body_len = cap // 2 + 5_000
    units = []
    for i in range(2):
        header = (
            f"diff --git a/src/f{i}.py b/src/f{i}.py\n"
            f"index 000..001 100644\n"
            f"--- a/src/f{i}.py\n"
            f"+++ b/src/f{i}.py\n"
            f"@@ -1 +1 @@\n"
            f"-old\n"
        )
        body = "+" + "x" * (unit_body_len - len(header) - 2) + "\n"
        units.append(header + body)
    return "".join(units)


def test_review_diff_uses_scoper() -> None:
    """Oversized diff: prompt contains scoped omission announcement, not old marker."""
    big_diff = _make_oversized_diff(_DIFF_HARD_CAP_CHARS)
    assert len(big_diff) > _DIFF_HARD_CAP_CHARS
    captured: list[str] = []

    def _stub_call(prompt: str, *, pr_number: int | None = None) -> tuple:
        captured.append(prompt)
        r = MagicMock()
        r.text = '{"verdict": "APPROVE", "summary": "ok", "comments": []}'
        r.model_used = "test"
        r.elapsed_s = 0.1
        r.tokens_used = 10
        r.trace = []
        return (ReviewVerdict.APPROVE, "ok", [], r)

    reviewer = Architect(loop=MagicMock())
    with (
        patch.object(reviewer, "_call_with_retry", _stub_call),
        patch.object(reviewer, "_render_prompt", lambda diff, **_kw: diff),
    ):
        reviewer.review_diff(big_diff)
    assert len(captured) == 1
    assert "[... diff truncated" not in captured[0]
    # Omission announcement is present regardless of format (implementation may vary)
    assert any(
        marker in captured[0]
        for marker in ("[diff scoped:", "OMITTED FILES", "not included due to", "src/f1.py")
    )


def test_review_diff_logs_scoping() -> None:
    """Oversized diff: _log.warning fired with event='review.diff_scoped' and n_omitted>0."""
    import ferova.review.reviewer as _mod

    big_diff = _make_oversized_diff(_DIFF_HARD_CAP_CHARS)
    assert len(big_diff) > _DIFF_HARD_CAP_CHARS

    def _stub_call(prompt: str, *, pr_number: int | None = None) -> tuple:
        r = MagicMock()
        r.text = '{"verdict": "APPROVE", "summary": "ok", "comments": []}'
        r.model_used = "test"
        r.elapsed_s = 0.1
        r.tokens_used = 10
        r.trace = []
        return (ReviewVerdict.APPROVE, "ok", [], r)

    fake_log = MagicMock()
    reviewer = Architect(loop=MagicMock())
    with (
        patch.object(reviewer, "_call_with_retry", _stub_call),
        patch.object(reviewer, "_render_prompt", lambda diff, **_kw: diff),
        patch.object(_mod, "_log", fake_log),
    ):
        reviewer.review_diff(big_diff)
    warning_events = [c.args[0] for c in fake_log.warning.call_args_list]
    assert "review.diff_scoped" in warning_events, (
        f"Expected 'review.diff_scoped' warning, got: {warning_events}"
    )
    scoped_calls = [c for c in fake_log.warning.call_args_list if c.args[0] == "review.diff_scoped"]
    assert scoped_calls[0].kwargs["n_omitted"] > 0
