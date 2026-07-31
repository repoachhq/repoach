"""Unit tests for the AST-based silent-except scanner.

Exercises :mod:`repoach.lint.no_silent_except` against in-memory
fixtures so the rule itself is tested independently of the ratchet
baseline.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from repoach.lint.no_silent_except import scan_file, summarise


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


class TestNonLoggerCallDoesNotClearHandler:
    """SP-LINT-GATE-SOUNDNESS G1 — receiver-blind log fallback is closed.

    A call whose terminal attribute spells a logging method name no
    longer clears a handler unless its receiver is rooted in a real
    logger object.
    """

    def test_non_logger_call_does_not_clear_handler(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:
                    cache.info()
                    pass
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "pass"

    def test_bare_log_call_on_non_logger_name_does_not_clear_handler(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except:
                    q.log(x)
                    return None
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "return"

    def test_bound_exception_not_referenced_by_logger_call_is_flagged(self, tmp_path: Path) -> None:
        """A2 tightening: binding ``exc`` but never referencing it is not honest logging."""
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception as exc:
                    logger.error("g_failed")
                    pass
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1

    def test_logger_call_referencing_bound_exception_clears_handler(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except ValueError as exc:
                    _log.warning("x", error=str(exc))
                    pass
            """,
        )
        assert scan_file(path) == []

    def test_no_binding_logger_call_still_clears_handler(self, tmp_path: Path) -> None:
        """A1: a handler with no ``as`` binding is cleared by any qualifying logger call."""
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


class TestContextlibSuppressFlagged:
    """SP-LINT-GATE-SOUNDNESS G2 — ``contextlib.suppress`` blindspot is closed."""

    def test_contextlib_suppress_flagged(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            import contextlib

            def f():
                with contextlib.suppress(Exception):
                    risky()
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "suppress"

    def test_bare_suppress_import_flagged(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            from contextlib import suppress

            def f():
                with suppress(OSError):
                    risky()
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "suppress"

    def test_suppress_with_logging_body_is_ok(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            import contextlib

            def f():
                with contextlib.suppress(OSError):
                    _log.warning("risky_failed")
                    risky()
            """,
        )
        assert scan_file(path) == []

    def test_non_suppress_with_block_is_not_flagged(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                with open("x") as fh:
                    fh.read()
            """,
        )
        assert scan_file(path) == []

    def test_standalone_allow_directive_above_suppress_suppresses(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            import contextlib

            def f():
                # repoach: allow-silent-except reason="narrow FS race, best-effort cleanup"
                with contextlib.suppress(OSError):
                    risky()
            """,
        )
        assert scan_file(path) == []


class TestAllowDirective:
    """The standalone ``# repoach: allow-silent-except reason="..."`` directive.

    SP-LINT-GATE-SOUNDNESS G3 moved the escape hatch off the
    ``except`` line (which the no-inline-comments gate forbids) onto
    a standalone comment line directly above it, and made the reason
    mandatory.
    """

    def test_standalone_allow_directive_above_except_suppresses(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                # repoach: allow-silent-except reason="parse failure is best-effort"
                except Exception:
                    return None
            """,
        )
        assert scan_file(path) == []

    def test_allow_directive_on_except_line_itself_does_not_suppress(self, tmp_path: Path) -> None:
        """The old inline placement (on the ``except`` line) no longer counts."""
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                except Exception:  # repoach: allow-silent-except reason="stale placement"
                    return None
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "return"

    def test_allow_directive_requires_reason(self, tmp_path: Path) -> None:
        """An empty or missing ``reason=`` fails closed: the violation still reports."""
        path = _write(
            tmp_path,
            "a.py",
            """
            def f():
                try:
                    g()
                # repoach: allow-silent-except reason=""
                except Exception:
                    return None
            """,
        )
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "return"

    def test_allow_directive_missing_entirely_does_not_suppress(self, tmp_path: Path) -> None:
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


class TestAllowDirectiveVariants:
    """The standalone allow directive tolerates whitespace variation."""

    @pytest.mark.parametrize(
        "directive",
        [
            '# repoach: allow-silent-except reason="best-effort"',
            '#   repoach:   allow-silent-except   reason="lots of spaces"',
            '#repoach: allow-silent-except reason="no space after hash"',
        ],
    )
    def test_directive_whitespace_variants(self, tmp_path: Path, directive: str) -> None:
        path = _write(
            tmp_path,
            "a.py",
            f"""
            def f():
                try:
                    g()
                {directive}
                except Exception:
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

    def test_unparseable_file_fails(self, tmp_path: Path) -> None:
        """A syntax-error file FAILS the gate instead of being silently skipped."""
        path = _write(tmp_path, "a.py", "def f(:\n    pass\n")
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "unparseable"

    def test_non_utf8_file_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "a.py"
        path.write_bytes(b"\xff\xfe\x00\x00bad bytes")
        violations = scan_file(path)
        assert len(violations) == 1
        assert violations[0].kind == "unparseable"


class TestScanRoots:
    """``scan`` is robust to missing roots and excludes ``__pycache__``."""

    def test_non_existent_root_is_skipped(self, tmp_path: Path) -> None:
        from repoach.lint.no_silent_except import scan

        missing = tmp_path / "does_not_exist"
        assert scan([missing]) == []

    def test_pycache_files_are_excluded(self, tmp_path: Path) -> None:
        from repoach.lint.no_silent_except import scan

        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        offender = pycache / "leftover.py"
        offender.write_text(
            "def f():\n    try:\n        g()\n    except Exception:\n        pass\n",
            encoding="utf-8",
        )
        assert scan([tmp_path]) == []

    def test_scan_aggregates_across_files(self, tmp_path: Path) -> None:
        from repoach.lint.no_silent_except import scan

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
