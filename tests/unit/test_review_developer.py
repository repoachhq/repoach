"""Tests for the autonomous NIM Developer agent."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from ferova.review.reviewer import (
    BotRole,
    Developer,
    _format_existing_files,
    _normalise_fixes,
    _parse_fix_plan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_developer_has_correct_role() -> None:
    dev = Developer(loop=MagicMock())
    assert dev.role == BotRole.DEVELOPER
    assert dev.persona_filename == "developer_0.2.0.md"


# ---------------------------------------------------------------------------
# _parse_fix_plan
# ---------------------------------------------------------------------------


def test_parse_fix_plan_strict_json() -> None:
    raw = json.dumps({"fixes": [{"path": "a.py", "new_content": "x"}]})
    parsed = _parse_fix_plan(raw)
    assert parsed is not None
    assert parsed["fixes"][0]["path"] == "a.py"


def test_parse_fix_plan_extracts_largest_blob_when_wrapped_in_prose() -> None:
    raw = (
        "Here is my plan:\n\n"
        + json.dumps({"fixes": [{"path": "b.py", "new_content": "y"}]})
        + "\n\nLet me know if this is correct."
    )
    parsed = _parse_fix_plan(raw)
    assert parsed is not None
    assert parsed["fixes"][0]["path"] == "b.py"


def test_parse_fix_plan_returns_none_on_garbage() -> None:
    assert _parse_fix_plan("nope") is None
    assert _parse_fix_plan("") is None


def test_parse_fix_plan_skips_dicts_without_fixes_key() -> None:
    raw = json.dumps({"summary": "ok", "commit_message": "m"})
    assert _parse_fix_plan(raw) is None


def test_parse_fix_plan_strips_markdown_fence() -> None:
    """``` ```json ... ``` ``` wrapping is unwrapped before parsing."""
    inner = json.dumps({"fixes": [{"path": "a.py", "new_content": "x"}]})
    raw = f"Here's my plan:\n\n```json\n{inner}\n```\n\nDone."
    parsed = _parse_fix_plan(raw)
    assert parsed is not None
    assert parsed["fixes"][0]["path"] == "a.py"


def test_parse_fix_plan_salvages_truncated_response() -> None:
    """A response cut by max_tokens mid-array still yields parseable plan."""
    truncated = (
        '{"fixes": [{"path": "a.py", "new_content": "ok\\n"},'
        ' {"path": "b.py", "new_content": "second'
    )
    parsed = _parse_fix_plan(truncated)
    assert parsed is not None
    # Salvage trims back to the last complete fix entry.
    assert any(f["path"] == "a.py" for f in parsed["fixes"])


def test_parse_fix_plan_salvages_unclosed_brackets() -> None:
    """Unclosed ``}`` ``]`` are auto-appended when no string is open."""
    truncated = '{"fixes": [{"path": "a.py", "new_content": "x"}'
    parsed = _parse_fix_plan(truncated)
    assert parsed is not None
    assert parsed["fixes"][0]["path"] == "a.py"


# ---------------------------------------------------------------------------
# _normalise_fixes
# ---------------------------------------------------------------------------


def test_normalise_fixes_drops_invalid_items() -> None:
    raw = [
        {"path": "ok.py", "new_content": "x"},
        {"path": "missing_content.py"},
        {"new_content": "missing_path"},
        {"path": 42, "new_content": "non-str-path"},
        {"path": "non_str_content.py", "new_content": 123},
        "not a dict",
        None,
    ]
    out = _normalise_fixes(raw)
    assert len(out) == 1
    assert out[0]["path"] == "ok.py"
    assert out[0]["rationale"] == ""


def test_normalise_fixes_returns_empty_for_non_list() -> None:
    assert _normalise_fixes("not a list") == []
    assert _normalise_fixes(None) == []
    assert _normalise_fixes({}) == []


def test_normalise_fixes_truncates_long_rationale() -> None:
    raw = [{"path": "x.py", "new_content": "y", "rationale": "x" * 800}]
    out = _normalise_fixes(raw)
    assert len(out[0]["rationale"]) == 400


# ---------------------------------------------------------------------------
# _format_existing_files
# ---------------------------------------------------------------------------


def test_format_existing_files_empty_returns_placeholder() -> None:
    out = _format_existing_files({})
    assert "no existing files" in out


def test_format_existing_files_renders_each_with_marker() -> None:
    out = _format_existing_files({"a.py": "code-a", "b.py": "code-b"})
    assert "=== a.py ===" in out
    assert "code-a" in out
    assert "=== b.py ===" in out
    assert "code-b" in out


def test_format_existing_files_truncates_huge_files() -> None:
    huge = "x" * 50_000
    out = _format_existing_files({"big.py": huge})
    assert "[... file truncated" in out
    # Cap is 32 KB; output ≈ 32 KB + markers, smaller than the input.
    assert len(out) < len(huge)


def test_format_existing_files_does_not_truncate_below_cap() -> None:
    """A 16 KB file fits under the 32 KB cap and is rendered intact."""
    medium = "y" * 16_000
    out = _format_existing_files({"med.py": medium})
    assert "[... file truncated" not in out
    assert "y" * 16_000 in out


# ---------------------------------------------------------------------------
# Developer.respond — happy path with mocked AgentLoop
# ---------------------------------------------------------------------------


def _stub_loop(text: str) -> MagicMock:
    """Build a AgentLoop mock whose run_oneshot returns ``text``."""
    loop = MagicMock()
    result = MagicMock()
    result.text = text
    result.model_used = "stub-model"
    result.elapsed_s = 0.0
    result.tokens_used = 0
    loop.run_oneshot.return_value = result
    return loop


@pytest.fixture(autouse=True)
def _redirect_parse_failed_dumps_to_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keep Developer/Coder parse-failed dumps off the repo's ``logs/``.

    SP-DEV-TEST-POLLUTION-LOGS-DIR — the stubbed ``_stub_loop("{}")``
    bypasses :func:`_developer_response_has_fixes` (MagicMock returns
    the stub regardless of ``accept_response``), so every test using
    it lands in the parse-failed branch and previously stamped a
    real 2-byte file in the working repo's ``logs/`` directory.  101
    such files accumulated between 2026-05-06 and 2026-05-24 before
    this fixture landed.  Redirecting the helper to ``tmp_path``
    is a belt-and-suspenders on top of the constructor's new
    ``logs_dir`` kwarg : even legacy tests that don't pass
    ``logs_dir=tmp_path`` cannot leak onto disk.
    """
    from ferova.review import reviewer

    real = reviewer._persist_parse_failed_response

    def _patched(*, role, identifier, raw, logs_dir=None):
        return real(
            role=role,
            identifier=identifier,
            raw=raw,
            logs_dir=logs_dir or tmp_path,
        )

    monkeypatch.setattr(reviewer, "_persist_parse_failed_response", _patched)


def test_developer_respond_returns_parsed_plan() -> None:
    payload = {
        "fixes": [
            {
                "path": "src/ferova/foo.py",
                "new_content": "def foo(): return 1\n",
                "rationale": "implements spec requirement A",
            }
        ],
        "commit_message": "feat(foo): add foo (SP-FOO)",
        "summary": "Implements requirement A from SP-FOO.",
    }
    loop = _stub_loop(json.dumps(payload))
    dev = Developer(loop=loop)
    out = dev.respond(
        spec_plan="# SP-FOO\n\nAdd foo.",
        existing_files={"src/ferova/__init__.py": "# package"},
        spec_id="SP-FOO",
    )
    assert len(out["fixes"]) == 1
    assert out["fixes"][0]["path"] == "src/ferova/foo.py"
    assert "SP-FOO" in out["commit_message"]
    assert out["model_used"] == "stub-model"


def test_developer_respond_returns_empty_fixes_on_unparseable_output() -> None:
    loop = _stub_loop("I cannot do this.")
    dev = Developer(loop=loop)
    out = dev.respond(spec_plan="# SP-X")
    assert out["fixes"] == []
    assert "I cannot" in out["summary"]


def test_developer_respond_substitutes_spec_plan_into_prompt() -> None:
    """Ensure {SPEC_PLAN} placeholder reaches the loop's run_oneshot."""
    loop = _stub_loop("{}")
    dev = Developer(loop=loop)
    dev.respond(spec_plan="# SP-MARKER unique-string-12345")
    args, kwargs = loop.run_oneshot.call_args
    rendered_prompt = args[0] if args else kwargs.get("prompt", "")
    assert "unique-string-12345" in rendered_prompt
    # Placeholder must NOT remain unsubstituted.
    assert "{SPEC_PLAN}" not in rendered_prompt


def test_developer_respond_includes_existing_files_block() -> None:
    loop = _stub_loop("{}")
    dev = Developer(loop=loop)
    dev.respond(
        spec_plan="# SP-X",
        existing_files={"src/foo.py": "EXISTING-MARKER-99"},
    )
    args, kwargs = loop.run_oneshot.call_args
    rendered_prompt = args[0] if args else kwargs.get("prompt", "")
    assert "EXISTING-MARKER-99" in rendered_prompt
    assert "src/foo.py" in rendered_prompt


@pytest.mark.parametrize(
    "missing_field",
    ["{REPO_TREE}", "{EXISTING_FILES}", "{SPEC_PLAN}"],
)
def test_developer_respond_substitutes_all_placeholders(missing_field: str) -> None:
    loop = _stub_loop("{}")
    dev = Developer(loop=loop)
    dev.respond(spec_plan="# SP-X", existing_files={"a": "b"}, repo_tree="src/\n  foo.py")
    args, kwargs = loop.run_oneshot.call_args
    rendered_prompt = args[0] if args else kwargs.get("prompt", "")
    assert missing_field not in rendered_prompt


# ---------------------------------------------------------------------------
# Edge cases flagged by the Tester NIM on PR #6 (SP-DEV bootstrap review)
# ---------------------------------------------------------------------------


def test_developer_respond_with_empty_spec_plan() -> None:
    """Empty spec → Developer still invoked, returns empty fixes from stub."""
    loop = _stub_loop("{}")
    dev = Developer(loop=loop)
    out = dev.respond(spec_plan="")
    assert out["fixes"] == []
    # Placeholder substitution still runs even with empty plan.
    args, _ = loop.run_oneshot.call_args
    assert "{SPEC_PLAN}" not in args[0]


def test_developer_respond_with_no_existing_files_renders_placeholder() -> None:
    """When no existing files referenced, prompt has the placeholder line."""
    loop = _stub_loop("{}")
    dev = Developer(loop=loop)
    dev.respond(spec_plan="# SP-X", existing_files=None)
    args, _ = loop.run_oneshot.call_args
    assert "no existing files" in args[0]


def test_developer_retries_on_transient_nim_error(monkeypatch) -> None:
    """First call raises APIConnectionError, second succeeds → no failure."""
    import time as _time

    from openai import APIConnectionError

    loop = MagicMock()
    success_result = MagicMock()
    success_result.text = (
        '{"fixes": [{"path": "a.py", "new_content": "x"}], "commit_message": "m", "summary": "s"}'
    )
    success_result.model_used = "stub"
    success_result.elapsed_s = 0.0
    success_result.tokens_used = 0

    call_count = {"n": 0}

    def _flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise APIConnectionError(request=MagicMock())
        return success_result

    loop.run_oneshot.side_effect = _flaky
    monkeypatch.setattr(_time, "sleep", lambda _s: None)

    dev = Developer(loop=loop)
    out = dev.respond(spec_plan="# SP-X")
    assert call_count["n"] == 2
    assert out["fixes"][0]["path"] == "a.py"


def test_developer_retry_handles_empty_backoffs_tuple(monkeypatch) -> None:
    """``_RETRY_BACKOFFS_S = ()`` — no attempts → returns empty plan, no infinite loop."""
    import time as _time

    loop = MagicMock()
    monkeypatch.setattr(_time, "sleep", lambda _s: None)

    dev = Developer(loop=loop)
    # Override the class attribute on the instance to an empty tuple.
    dev._RETRY_BACKOFFS_S = ()
    out = dev.respond(spec_plan="# SP-X")
    # No call attempts made → empty fix-plan with the exhaustion summary.
    assert loop.run_oneshot.call_count == 0
    assert out["fixes"] == []


def test_developer_retry_handles_spec_id_none(monkeypatch) -> None:
    """``spec_id=None`` is accepted (default) and logging handles it gracefully."""
    import time as _time

    from openai import APIConnectionError

    loop = MagicMock()
    loop.run_oneshot.side_effect = APIConnectionError(request=MagicMock())
    monkeypatch.setattr(_time, "sleep", lambda _s: None)

    dev = Developer(loop=loop)
    # Explicitly omit spec_id (default is None).
    out = dev.respond(spec_plan="# SP-X")
    assert out["fixes"] == []
    # Should still go through the full retry loop (3 attempts).
    assert loop.run_oneshot.call_count == 3


def test_developer_retry_catches_non_apiconnection_exceptions(monkeypatch) -> None:
    """Any exception (not just APIConnectionError) is caught and retried per policy."""
    import time as _time

    loop = MagicMock()

    call_count = {"n": 0}
    success_result = MagicMock()
    success_result.text = (
        '{"fixes": [{"path": "a.py", "new_content": "x"}], "commit_message": "m", "summary": "s"}'
    )
    success_result.model_used = "stub"
    success_result.elapsed_s = 0.0
    success_result.tokens_used = 0

    def _flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # A non-NIM exception still triggers the retry path.
            raise ValueError("unexpected non-API error")
        return success_result

    loop.run_oneshot.side_effect = _flaky
    monkeypatch.setattr(_time, "sleep", lambda _s: None)

    dev = Developer(loop=loop)
    out = dev.respond(spec_plan="# SP-X")
    assert call_count["n"] == 2
    assert out["fixes"][0]["path"] == "a.py"


def test_developer_returns_empty_after_all_retries_exhausted(monkeypatch) -> None:
    """All 3 attempts raise → respond() returns empty fix-plan, doesn't crash."""
    import time as _time

    from openai import APIConnectionError

    loop = MagicMock()
    loop.run_oneshot.side_effect = APIConnectionError(request=MagicMock())
    monkeypatch.setattr(_time, "sleep", lambda _s: None)

    dev = Developer(loop=loop)
    out = dev.respond(spec_plan="# SP-X")
    assert out["fixes"] == []
    assert "exhausted" in out["summary"].lower()
    # 3 retry attempts (per _RETRY_BACKOFFS_S length).
    assert loop.run_oneshot.call_count == 3


def test_developer_respond_persists_token_usage_for_audit() -> None:
    """The L4 audit row needs model_used / elapsed_s / tokens_used."""
    loop = MagicMock()
    result = MagicMock()
    result.text = '{"fixes": [], "commit_message": "", "summary": "x"}'
    result.model_used = "qwen/qwen3-coder-480b-a35b-instruct"
    result.elapsed_s = 12.3
    result.tokens_used = 4567
    loop.run_oneshot.return_value = result

    out = Developer(loop=loop).respond(spec_plan="# SP-X")
    assert out["model_used"] == "qwen/qwen3-coder-480b-a35b-instruct"
    assert out["elapsed_s"] == 12.3
    assert out["tokens_used"] == 4567


def test_developer_respond_writes_parse_failed_dump_to_injected_logs_dir(
    tmp_path,
) -> None:
    """SP-DEV-TEST-POLLUTION-LOGS-DIR — ``Developer(logs_dir=X)`` lands dumps in X.

    Regression guard against the bug where Developer.respond hardcoded
    the repo's ``logs/`` directory and tests with ``_stub_loop("{}")``
    silently polluted the working tree (101 such files accumulated in
    2 weeks before the fixture-based safety net landed).
    """
    custom_dir = tmp_path / "custom-developer-logs"
    custom_dir.mkdir()
    loop = _stub_loop("{}")
    dev = Developer(loop=loop, logs_dir=custom_dir)

    dev.respond(spec_plan="# SP-X")

    written = list(custom_dir.glob("developer_parse_failed_*.txt"))
    assert len(written) == 1, f"expected one dump in {custom_dir}, got {written}"
    assert written[0].read_text(encoding="utf-8") == "{}"


def test_developer_loop_carries_the_thirty_turn_budget() -> None:
    """The agentic Developer gets 30 tool-call turns, not the 15 default.

    Both SP-DEV-STEP-PREFLIGHT dispatches (2026-07-04) exhausted the
    15-turn budget on read operations against the ~1,000-line
    dev_runner.py before writing anything; this pin keeps the raised
    budget from silently regressing.
    """
    import ferova.review.reviewer as reviewer_module

    assert reviewer_module._DEVELOPER_MAX_TURNS == 30
