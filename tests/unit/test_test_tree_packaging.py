"""SP-TEST-BASENAME-GATE — packaged test tree resolves colliding basenames.

Drives real subprocess ``pytest --collect-only`` invocations against
the actual repo tree (not synthetic fixtures) so the assertion covers
the exact failure mode described in the spec: pytest's ``prepend``
import mode raising ``import file mismatch`` when two same-named test
files are collected together. ``tests/__init__.py``,
``tests/unit/__init__.py``, and ``tests/integration/__init__.py``
(added by this spec) make pytest resolve each collected module under
its fully qualified dotted name (``tests.unit.test_x`` vs
``tests.integration.test_x``) instead of the bare basename, so the
collision class is eliminated regardless of how many colliding pairs
exist (see ``tests/unit/test_no_duplicate_test_basenames_gate.py`` for
the ratcheting count on that separate axis).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_collecting_colliding_basenames_together_succeeds() -> None:
    """The known-colliding pair collects together with zero import file mismatch.

    Fails on pre-change code (no ``__init__.py`` under any of the
    three test directories), where the same invocation exits non-zero
    with ``import file mismatch`` in its output.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "tests/unit/test_review_dev_runner.py",
            "tests/integration/test_review_dev_runner.py",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    combined_output = result.stdout + result.stderr
    assert "import file mismatch" not in combined_output, combined_output
    assert result.returncode == 0, combined_output


def test_bare_tree_collection_succeeds() -> None:
    """``pytest --collect-only tests/`` (the bare ``testpaths`` default) errors zero.

    Fails on pre-change code, where collecting the whole tree in one
    invocation raises ``import file mismatch`` for at least one of the
    6 colliding basenames.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    combined_output = result.stdout + result.stderr
    assert "import file mismatch" not in combined_output, combined_output
    assert "error during collection" not in combined_output.lower(), combined_output
    assert result.returncode == 0, combined_output
