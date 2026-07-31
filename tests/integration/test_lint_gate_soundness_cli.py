"""SP-LINT-GATE-SOUNDNESS (AC2) — the two lint CLIs end-to-end over a fixture tree.

Drives ``scripts/lint_no_silent_except.py`` and
``scripts/lint_no_inline_comments.py`` as real subprocesses (the path
the pre-commit hook and CI actually exercise) over a tmp fixture tree
containing the patterns the audit found blind, asserting the observed
exit code and stdout rather than a helper's in-process return value.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SILENT_EXCEPT_CLI = _REPO_ROOT / "scripts" / "lint_no_silent_except.py"
_INLINE_COMMENTS_CLI = _REPO_ROOT / "scripts" / "lint_no_inline_comments.py"


def _run(cli: Path, args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(cli), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _write_fixture_tree(tmp_path: Path) -> Path:
    """Build (a) a receiver-blind log fallback, (b) a silent ``contextlib.suppress``,
    (c) a syntax-error file, all under one scanned root.
    """
    root = tmp_path / "fixture_src"
    root.mkdir()
    (root / "non_logger_fallback.py").write_text(
        textwrap.dedent(
            """
            def f():
                try:
                    g()
                except:
                    cache.info()
                    pass
            """
        ),
        encoding="utf-8",
    )
    (root / "suppressed.py").write_text(
        textwrap.dedent(
            """
            import contextlib

            def f():
                with contextlib.suppress(Exception):
                    risky()
            """
        ),
        encoding="utf-8",
    )
    (root / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    return root


class TestSilentExceptCliCatchesEnumeratedBlindspots:
    """Scenarios (a)-(c): each is reported and the process exits non-zero."""

    def test_fixture_tree_all_reported_and_exits_nonzero(self, tmp_path: Path) -> None:
        root = _write_fixture_tree(tmp_path)
        result = _run(_SILENT_EXCEPT_CLI, ["--root", str(root)], cwd=tmp_path)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "non_logger_fallback.py" in result.stdout
        assert "[pass]" in result.stdout
        assert "suppressed.py" in result.stdout
        assert "[suppress]" in result.stdout
        assert "broken.py" in result.stdout
        assert "[unparseable]" in result.stdout


class TestInlineCommentsCliCatchesUnparseable:
    """The inline-comments gate also fails loudly on the syntax-error file."""

    def test_syntax_error_file_reported_and_exits_nonzero(self, tmp_path: Path) -> None:
        root = tmp_path / "fixture_src"
        root.mkdir()
        (root / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

        result = _run(_INLINE_COMMENTS_CLI, ["--root", str(root)], cwd=tmp_path)

        assert result.returncode == 1, result.stdout + result.stderr
        assert "broken.py" in result.stdout
        assert "[unparseable]" in result.stdout


class TestMissingRootsExitNonzero:
    """Scenario (d): zero resolved roots fails CLOSED with exit code 2."""

    def test_missing_roots_exit_nonzero(self, tmp_path: Path) -> None:
        missing_a = tmp_path / "does_not_exist_a"
        missing_b = tmp_path / "does_not_exist_b"

        result = _run(
            _SILENT_EXCEPT_CLI,
            ["--root", str(missing_a), "--root", str(missing_b)],
            cwd=tmp_path,
        )

        assert result.returncode == 2, result.stdout + result.stderr
        assert str(missing_a) in result.stdout
        assert str(missing_b) in result.stdout

    def test_inline_comments_missing_roots_exit_nonzero(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist"

        result = _run(_INLINE_COMMENTS_CLI, ["--root", str(missing)], cwd=tmp_path)

        assert result.returncode == 2, result.stdout + result.stderr
        assert str(missing) in result.stdout


class TestRootResolutionIsRepoRootRelativeNotCwdRelative:
    """G5's other half: a relative ``--root`` resolves against the repo root
    (walked up from the script), never against the process's CWD.

    Discriminator: a decoy directory that exists only under the tmp CWD and
    NOT under the real repo root. The pre-fix CLI resolved bare relative
    ``Path(r)`` values against CWD, so it would happily scan the decoy
    (and catch its silent-except) from the wrong-CWD invocation. The fixed
    CLI resolves against the repo root, finds no such directory there, and
    fails CLOSED (exit 2) instead of silently scanning whatever the CWD
    happens to contain.
    """

    def test_relative_root_resolves_against_repo_root_not_cwd(self, tmp_path: Path) -> None:
        decoy = tmp_path / "decoy_only_in_tmp_cwd"
        decoy.mkdir()
        (decoy / "silent.py").write_text(
            textwrap.dedent(
                """
                def f():
                    try:
                        g()
                    except Exception:
                        pass
                """
            ),
            encoding="utf-8",
        )

        result = _run(_SILENT_EXCEPT_CLI, ["--root", "decoy_only_in_tmp_cwd"], cwd=tmp_path)

        assert result.returncode == 2, result.stdout + result.stderr
        assert "decoy_only_in_tmp_cwd" in result.stdout
