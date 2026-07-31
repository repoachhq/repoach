"""SP-SAFE-MERGE-SKIP-WARN — ``--skip-review`` is a loud, confirmed bypass.

Audit 2026-07-13 finding M12: ``scripts/safe_merge.sh --skip-review``
used to disable both the review-bot run (step 4) and the pure
evidence-first merge gate (step 5) with only a quiet parenthetical,
and combined with ``--skip-tests`` reached the merge with no automated
gate and no operator prompt at all.

These tests drive the real script as a subprocess (the path an
operator actually exercises), stubbing only the true external
boundaries (``gh`` and ``git`` on ``PATH``) so the observed behaviour
is the script's own control flow, not a helper's in-process return
value. ``scripts/ci_local.sh`` is likewise faked inside a self-contained
fixture tree — the script's own ``cd "$(dirname "$0")/.."`` resolves
against wherever the copied script lives, so the fixture's fake is the
one it calls, never the real, slow one.

- ``test_skip_review_warns_and_halts_without_confirm`` (AC1 + AC2
  refusal case): the warning names step 4 and step 5, an unconfirmed
  bypass (empty stdin) exits non-zero, and ``gh`` — hence the ``pr
  merge`` fake — is never invoked.
- ``test_skip_review_proceeds_with_sentinel`` (AC2 proceed case): the
  correct sentinel clears the warning and the run proceeds all the way
  to the ``gh pr merge`` fake.

Reverting the confirmation gate (``git stash`` on the ``scripts/``
change) makes both tests fail: the old code printed only a quiet
parenthetical and never blocked on stdin, so the halt case would not
exit non-zero before invoking ``gh``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_SAFE_MERGE = _REPO_ROOT / "scripts" / "safe_merge.sh"

_FAKE_HEAD_BRANCH = "fake-head-branch"
_FAKE_SHA = "cafefeed0000cafefeed0000cafefeed0000cafe"

_FAKE_GH = f"""#!/usr/bin/env bash
set -euo pipefail
printf 'gh %s\\n' "$*" >> "$FAKE_CALL_LOG"
case "$*" in
    "pr merge"*)
        printf 'MERGE_FAKE_INVOKED\\n' >> "$FAKE_CALL_LOG"
        exit 0
        ;;
    "pr view"*"--json baseRefName"*)
        echo "develop"
        ;;
    "pr view"*"--json state"*)
        echo "OPEN"
        ;;
    "pr view"*"--json headRefName"*)
        echo "{_FAKE_HEAD_BRANCH}"
        ;;
    "pr view"*"--json headRefOid"*)
        echo "{_FAKE_SHA}"
        ;;
    "pr checkout"*)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
"""

_FAKE_GIT = f"""#!/usr/bin/env bash
set -euo pipefail
printf 'git %s\\n' "$*" >> "$FAKE_CALL_LOG"
case "$1" in
    symbolic-ref)
        echo "{_FAKE_HEAD_BRANCH}"
        ;;
    ls-remote)
        printf '{_FAKE_SHA}\\trefs/heads/{_FAKE_HEAD_BRANCH}\\n'
        ;;
    diff|fetch|merge|checkout|status)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
"""

_FAKE_CI_LOCAL = """#!/usr/bin/env bash
echo "fake ci_local ok"
exit 0
"""

_FAKE_REPOACH = f"""#!/usr/bin/env bash
set -euo pipefail
printf 'repoach %s\\n' "$*" >> "$FAKE_CALL_LOG"
case "$*" in
    "review pr"*)
        exit 0
        ;;
    "review gate"*)
        printf '{{"head_sha": "%s", "facts": {{}}}}' "{_FAKE_SHA}"
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
"""


def _make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _build_fixture_repo(tmp_path: Path) -> Path:
    fixture_root = tmp_path / "fixture_repo"
    fixture_scripts = fixture_root / "scripts"
    fixture_scripts.mkdir(parents=True)
    shutil.copyfile(_REAL_SAFE_MERGE, fixture_scripts / "safe_merge.sh")
    (fixture_scripts / "safe_merge.sh").chmod(0o755)
    _make_executable(fixture_scripts / "ci_local.sh", _FAKE_CI_LOCAL)
    return fixture_root


def _build_fake_bin(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir()
    _make_executable(fake_bin / "gh", _FAKE_GH)
    _make_executable(fake_bin / "git", _FAKE_GIT)
    _make_executable(fake_bin / "repoach", _FAKE_REPOACH)
    return fake_bin


def _run_safe_merge(
    fixture_root: Path,
    fake_bin: Path,
    call_log: Path,
    args: list[str],
    stdin_text: str,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_CALL_LOG"] = str(call_log)
    return subprocess.run(
        [str(fixture_root / "scripts" / "safe_merge.sh"), *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        cwd=fixture_root,
        timeout=30,
    )


class TestSkipReviewWarnsAndHaltsWithoutConfirm:
    """AC1 + AC2 (refusal branch): unconfirmed ``--skip-review`` halts loud."""

    def test_skip_review_warns_and_halts_without_confirm(self, tmp_path: Path) -> None:
        fixture_root = _build_fixture_repo(tmp_path)
        fake_bin = _build_fake_bin(tmp_path)
        call_log = tmp_path / "calls.log"

        result = _run_safe_merge(
            fixture_root,
            fake_bin,
            call_log,
            ["42", "--skip-review"],
            stdin_text="",
        )

        combined = result.stdout + result.stderr
        assert result.returncode == 1, combined
        assert "step 4" in combined, combined
        assert "step 5" in combined, combined
        assert "Aborted" in combined, combined
        assert not call_log.exists() or "MERGE_FAKE_INVOKED" not in call_log.read_text()

    def test_skip_review_wrong_sentinel_halts(self, tmp_path: Path) -> None:
        fixture_root = _build_fixture_repo(tmp_path)
        fake_bin = _build_fake_bin(tmp_path)
        call_log = tmp_path / "calls.log"

        result = _run_safe_merge(
            fixture_root,
            fake_bin,
            call_log,
            ["42", "--skip-review"],
            stdin_text="no\n",
        )

        combined = result.stdout + result.stderr
        assert result.returncode == 1, combined
        assert "Aborted" in combined, combined
        assert not call_log.exists() or "MERGE_FAKE_INVOKED" not in call_log.read_text()

    def test_skip_review_with_skip_tests_names_lint_only_ci(self, tmp_path: Path) -> None:
        fixture_root = _build_fixture_repo(tmp_path)
        fake_bin = _build_fake_bin(tmp_path)
        call_log = tmp_path / "calls.log"

        result = _run_safe_merge(
            fixture_root,
            fake_bin,
            call_log,
            ["42", "--skip-review", "--skip-tests"],
            stdin_text="",
        )

        combined = result.stdout + result.stderr
        assert result.returncode == 1, combined
        assert "lint-only" in combined, combined
        assert not call_log.exists() or "MERGE_FAKE_INVOKED" not in call_log.read_text()


class TestSkipReviewProceedsWithSentinel:
    """AC2 (proceed branch): the correct sentinel clears the warning."""

    def test_skip_review_proceeds_with_sentinel(self, tmp_path: Path) -> None:
        fixture_root = _build_fixture_repo(tmp_path)
        fake_bin = _build_fake_bin(tmp_path)
        call_log = tmp_path / "calls.log"

        result = _run_safe_merge(
            fixture_root,
            fake_bin,
            call_log,
            ["42", "--skip-review"],
            stdin_text="I understand\n",
        )

        combined = result.stdout + result.stderr
        assert "Aborted" not in combined, combined
        assert "Bypass confirmed by operator" in combined, combined
        assert call_log.exists(), combined
        assert "MERGE_FAKE_INVOKED" in call_log.read_text(), combined
        assert result.returncode == 0, combined

    def test_skip_review_proceeds_with_i_understand_flag(self, tmp_path: Path) -> None:
        fixture_root = _build_fixture_repo(tmp_path)
        fake_bin = _build_fake_bin(tmp_path)
        call_log = tmp_path / "calls.log"

        result = _run_safe_merge(
            fixture_root,
            fake_bin,
            call_log,
            ["42", "--skip-review", "--i-understand-skip-review"],
            stdin_text="",
        )

        combined = result.stdout + result.stderr
        assert "Aborted" not in combined, combined
        assert "--i-understand-skip-review passed" in combined, combined
        assert call_log.exists(), combined
        assert "MERGE_FAKE_INVOKED" in call_log.read_text(), combined
        assert result.returncode == 0, combined


class TestNoSkipFlagsUnchanged:
    """Nominal path (no skip flags) never shows the bypass warning."""

    def test_no_flags_never_reads_stdin_before_gh_calls(self, tmp_path: Path) -> None:
        fixture_root = _build_fixture_repo(tmp_path)
        fake_bin = _build_fake_bin(tmp_path)
        call_log = tmp_path / "calls.log"

        result = _run_safe_merge(
            fixture_root,
            fake_bin,
            call_log,
            ["42"],
            stdin_text="",
        )

        combined = result.stdout + result.stderr
        assert "merge-gate bypass requested" not in combined, combined
        assert call_log.exists(), combined
        assert "gh pr view 42 --json baseRefName" in call_log.read_text(), combined
