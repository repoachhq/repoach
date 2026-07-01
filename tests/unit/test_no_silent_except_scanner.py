"""Unit tests for the AST-based silent-except scanner.

Exercises :mod:`ferova.lint.no_silent_except` against in-memory
fixtures so the rule itself is tested independently of the ratchet
baseline.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ferova.lint.no_silent_except import scan_file, summarise


def _write(tmp_path: Path, name: str, body: str) -> Path:
    """Helper to drop a Python snippet at ``tmp_path/name`` and return it."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class TestSilentTrailers:
    """The scanner flags handlers ending in pass / return / continue."""

    def test_flags_pass(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    pass
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "pass"

    def test_flags_return_none(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    return None
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "return"
        assert violations[0].snippet == "return None"

    def test_flags_bare_return(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    return
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].snippet == "return"

    @pytest.mark.parametrize(
        "trailer", ["return []", "return {}", "return ()", "return False", "return 0", 'return ""']
    )
    def test_flags_empty_or_falsy_returns(self, tmp_path: Path, trailer: str) -> None:
        path = _write(
            tmp_path,
            "a.py",
            f"""
            def f():
                try:
                    g()
                except Exception:
                    {trailer}
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "return"

    def test_flags_continue(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                for x in xs:
                    try:
                        g(x)
                    except Exception:
                        continue
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "continue"


class TestTrailerBypasses:
    """SP-LINT-SILENT-TRAILERS — the ``...`` and assignment bypasses are closed."""

    def test_ellipsis_trailer_flagged(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    ...
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "ellipsis"
        assert violations[0].snippet == "..."

    @pytest.mark.parametrize(
        "trailer",
        [
            "result = None",
            "result = False",
            "result = 0",
            'result = ""',
            "items = []",
            "mapping = {}",
            "pair = ()",
        ],
    )
    def test_silent_assignment_trailer_flagged(self, tmp_path: Path, trailer: str) -> None:
        path = _write(
            tmp_path,
            "a.py",
            f"""
            def f():
                try:
                    g()
                except Exception:
                    {trailer}
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "assign"
        assert violations[0].snippet == trailer.replace('""', "''")

    def test_silent_annassign_trailer_flagged(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    items: list = []
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "assign"

    def test_computed_assignment_allowed(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    result = compute_fallback()
            """,
        )
        assert scan_file(path) == []

    def test_logged_ellipsis_trailer_is_ok(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    _log.warning("g_failed")
                    ...
            """,
        )
        assert scan_file(path) == []

    def test_logged_assignment_trailer_is_ok(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    _log.warning("g_failed")
                    result = None
            """,
        )
        assert scan_file(path) == []

    def test_bare_annotation_is_not_flagged(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    result: int
            """,
        )
        assert scan_file(path) == []

    def test_summary_buckets_new_kinds(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    ...
                try:
                    g()
                except Exception:
                    result = None
            """,
        )
        counts = summarise(scan_file(path))
        assert counts["ellipsis"] == 1
        assert counts["assign"] == 1
        assert counts["total"] == 2


class TestNonSilentHandlers:
    """A handler with a log call or a raise is not flagged."""

    def test_handler_with_log_call_is_ok(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception as exc:
                    _log.warning("g_failed", error=str(exc))
                    return None
            """,
        )
        assert scan_file(path) == []

    def test_handler_with_logger_call_is_ok(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    logger.error("g_failed")
                    return None
            """,
        )
        assert scan_file(path) == []

    def test_handler_with_self_log_call_is_ok(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            class C:
                def f(self):
                    try:
                        g()
                    except Exception:
                        self._log.warning("g_failed")
                        return None
            """,
        )
        assert scan_file(path) == []

    def test_handler_with_raise_is_ok(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    raise
            """,
        )
        assert scan_file(path) == []

    def test_handler_with_complex_body_is_ok(self, tmp_path: Path) -> None:
        """A handler whose final stmt is not a silent trailer is not flagged."""
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    if condition:
                        do_one()
                    else:
                        do_two()
            """,
        )
        assert scan_file(path) == []


class TestAllowDirective:
    """The ``# allow-silent-except`` directive suppresses the violation."""

    def test_allow_on_except_line_suppresses(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:  # allow-silent-except: parse failure is best-effort
                    return None
            """,
        )
        assert scan_file(path) == []


class TestAllowDirectiveVariants:
    """The allow directive tolerates case + whitespace variations."""

    @pytest.mark.parametrize(
        "directive",
        [
            "# allow-silent-except: best-effort",
            "# ALLOW-SILENT-EXCEPT: shouting works too",
            "#allow-silent-except: no space after hash",
            "#   allow-silent-except: lots of spaces",
        ],
    )
    def test_directive_case_and_whitespace_variants(self, tmp_path: Path, directive: str) -> None:
        path = _write(
            tmp_path,
            "a.py",
            f"""
            def f():
                try:
                    g()
                except Exception:  {directive}
                    return None
            """,
        )
        assert scan_file(path) == []


class TestNestedHandlers:
    """Each ExceptHandler is judged independently of its siblings."""

    def test_outer_logged_inner_silent_flags_inner_only(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    try:
                        g()
                    except ValueError:
                        return None
                except Exception:
                    _log.warning("outer_failed")
                    return None
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "return"
        assert violations[0].snippet == "return None"

    def test_both_silent_flags_both(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    try:
                        g()
                    except ValueError:
                        return None
                except Exception:
                    pass
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 2


class TestFileResilience:
    """``scan_file`` never raises on adversarial inputs."""

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "a.py", "")
        assert scan_file(path) == []

    def test_file_with_no_try_blocks_returns_empty(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                return 42
            """,
        )
        assert scan_file(path) == []

    def test_syntax_error_file_is_silently_skipped(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "a.py", "def f(:\n    pass\n")
        assert scan_file(path) == []

    def test_non_utf8_file_is_silently_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "a.py"
        path.write_bytes(b"\xff\xfe\x00\x00bad bytes")
        assert scan_file(path) == []


class TestScanRoots:
    """``scan`` is robust to missing roots and excludes ``__pycache__``."""

    def test_non_existent_root_is_skipped(self, tmp_path: Path) -> None:
        from ferova.lint.no_silent_except import scan

        missing = tmp_path / "does_not_exist"
        assert scan([missing]) == []

    def test_pycache_files_are_excluded(self, tmp_path: Path) -> None:
        from ferova.lint.no_silent_except import scan

        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        offender = pycache / "leftover.py"
        offender.write_text(
            "def f():\n    try:\n        g()\n    except Exception:\n        pass\n",
            encoding="utf-8",
        )
        assert scan([tmp_path]) == []

    def test_scan_aggregates_across_files(self, tmp_path: Path) -> None:
        from ferova.lint.no_silent_except import scan

        _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    pass
            """,
        )
        _write(
            tmp_path,
            "b.py",
            """
            def h():
                try:
                    g()
                except Exception:
                    return None
            """,
        )
        violations = scan([tmp_path])
        assert len(violations) == 2
        assert {v.path.name for v in violations} == {"a.py", "b.py"}


class TestSummary:
    """The summary helper buckets violations by trailer kind."""

    def test_summary_buckets_by_kind(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    pass
                try:
                    g()
                except Exception:
                    return None
            """,
        )
        counts = summarise(scan_file(path))
        assert counts["pass"] == 1
        assert counts["return"] == 1
        assert counts["continue"] == 0
        assert counts["total"] == 2
        assert counts["files"] == 1
