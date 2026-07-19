"""Unit tests for SP-DEV-STEP-CONTEXT — import gate, lint gate, spec-in-brief.

The import gate must turn a hallucinated ``ferova`` import into
directive feedback (what exists, where the names live); the repo lint
gate must surface the house rules before the commit hook does; the
step brief must carry the spec verbatim so plan actions that reference
it are resolvable. All thresholds derive from measured fixtures
(test-arithmetic law).
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.dev_runner import (
    _BRIEF_SPEC_CAP_CHARS,
    build_step_brief,
    run_repo_lint_gates,
)
from repoach.review.import_gate import check_imports
from repoach.review.plan import ActionPlan, PlanStep


def _seed_module(repo: Path, rel: str, content: str) -> str:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return rel


def _seed_findings_package(repo: Path) -> None:
    _seed_module(repo, "src/repoach/__init__.py", "")
    _seed_module(repo, "src/repoach/review/__init__.py", "")
    _seed_module(
        repo,
        "src/repoach/review/findings.py",
        "class Finding:\n    pass\n\n\nTAXONOMY = ()\n",
    )


def test_missing_module_directive_report(tmp_path: Path) -> None:
    _seed_findings_package(tmp_path)
    candidate = _seed_module(
        tmp_path,
        "src/repoach/review/bridge.py",
        "from repoach.review.models import Finding\n",
    )
    ok, report = check_imports(tmp_path, [candidate])
    assert ok is False
    assert "repoach.review.models" in report
    assert "findings" in report


def test_missing_name_located(tmp_path: Path) -> None:
    _seed_findings_package(tmp_path)
    candidate = _seed_module(
        tmp_path,
        "src/repoach/review/bridge.py",
        "from repoach.review.findings import Verdict\n",
    )
    ok, report = check_imports(tmp_path, [candidate])
    assert ok is False
    assert "'Verdict' is not defined in 'repoach.review.findings'" in report


def test_clean_imports_pass(tmp_path: Path) -> None:
    _seed_findings_package(tmp_path)
    candidate = _seed_module(
        tmp_path,
        "src/repoach/review/bridge.py",
        "from repoach.review.findings import TAXONOMY, Finding\n",
    )
    ok, report = check_imports(tmp_path, [candidate])
    assert ok is True
    assert report == ""


def test_relative_imports_resolved(tmp_path: Path) -> None:
    _seed_findings_package(tmp_path)
    good = _seed_module(
        tmp_path,
        "src/repoach/review/bridge.py",
        "from .findings import Finding\n",
    )
    bad = _seed_module(
        tmp_path,
        "src/repoach/review/bridge_bad.py",
        "from .models import Finding\n",
    )
    assert check_imports(tmp_path, [good])[0] is True
    ok, report = check_imports(tmp_path, [bad])
    assert ok is False
    assert "repoach.review.models" in report


def test_third_party_ignored(tmp_path: Path) -> None:
    _seed_findings_package(tmp_path)
    candidate = _seed_module(
        tmp_path,
        "src/repoach/review/bridge.py",
        "import json\nfrom pydantic import BaseModel\nfrom nonexistent_pkg import thing\n",
    )
    assert check_imports(tmp_path, [candidate])[0] is True


def test_lint_gate_flags_inline_comment_and_silent_except(tmp_path: Path) -> None:
    candidate = _seed_module(
        tmp_path,
        "src/repoach/review/dirty.py",
        "x = 1  # inline comment\ntry:\n    y = 2\nexcept Exception:\n    pass\n",
    )
    ok, report = run_repo_lint_gates(tmp_path, [candidate])
    assert ok is False
    assert "inline comment forbidden" in report
    assert "silent except forbidden" in report
    assert "House rules" in report


def test_lint_gate_passes_clean_file(tmp_path: Path) -> None:
    candidate = _seed_module(
        tmp_path,
        "src/repoach/review/clean.py",
        '"""Clean module."""\n\nx = 1\n',
    )
    ok, report = run_repo_lint_gates(tmp_path, [candidate])
    assert ok is True
    assert report == ""


def _one_step_plan() -> tuple[ActionPlan, PlanStep]:
    step = PlanStep(
        index=1,
        title="Do the step",
        files=[
            "src/repoach/foo.py",
            "tests/unit/test_foo.py",
            "tests/integration/test_foo_flow.py",
        ],
        action="Apply the fixes.",
        commit_message="feat(foo): step",
        done_when="gates green",
        unit_tests=["tests/unit/test_foo.py::test_brief_carries_spec_section"],
    )
    plan = ActionPlan(
        spec_id="SP-FOO",
        title="Sample",
        summary="One step.",
        steps=[step],
        integration_tests=["tests/integration/test_foo_flow.py"],
    )
    return plan, step


def test_brief_carries_spec_section() -> None:
    plan, step = _one_step_plan()
    spec_text = "# SP-FOO — the spec\n\nRequired imports: from .findings import Finding"
    brief = build_step_brief(plan, step, spec_markdown=spec_text)
    assert "## Source spec (verbatim — the plan's authority)" in brief
    assert "Required imports" in brief


def test_brief_spec_section_capped() -> None:
    plan, step = _one_step_plan()
    oversized = "x" * (_BRIEF_SPEC_CAP_CHARS + 500)
    brief = build_step_brief(plan, step, spec_markdown=oversized)
    section = brief.split("## Source spec (verbatim — the plan's authority)")[1]
    assert len(section) <= _BRIEF_SPEC_CAP_CHARS + 10


def test_brief_without_spec_unchanged() -> None:
    plan, step = _one_step_plan()
    assert "Source spec" not in build_step_brief(plan, step)


def test_submodule_from_import_is_accepted(tmp_path: Path) -> None:
    """``from <package> import <submodule>`` resolves via the module file.

    Tech-debt ledger entry 25 (2026-07-02 dogfood): the gate consulted
    only ``__init__`` top-level names, refusing the perfectly valid
    submodule form and blocking a legitimate Developer step twice.
    """
    _seed_findings_package(tmp_path)
    candidate = _seed_module(
        tmp_path,
        "tests/unit/test_probe.py",
        "from repoach.review import findings\n",
    )
    ok, report = check_imports(tmp_path, [candidate])
    assert ok is True
    assert report == ""


def test_subpackage_from_import_is_accepted(tmp_path: Path) -> None:
    """``from repoach import review`` resolves via the package __init__."""
    _seed_findings_package(tmp_path)
    candidate = _seed_module(
        tmp_path,
        "tests/unit/test_probe.py",
        "from repoach import review\n",
    )
    ok, report = check_imports(tmp_path, [candidate])
    assert ok is True
    assert report == ""


def test_missing_name_is_still_reported(tmp_path: Path) -> None:
    """A name that is neither defined nor a submodule still violates."""
    _seed_findings_package(tmp_path)
    candidate = _seed_module(
        tmp_path,
        "tests/unit/test_probe.py",
        "from repoach.review import does_not_exist\n",
    )
    ok, report = check_imports(tmp_path, [candidate])
    assert ok is False
    assert "does_not_exist" in report


def test_facade_reexported_name_is_accepted(tmp_path: Path) -> None:
    """A name imported through a package façade counts as defined there.

    The gate once refused ``from repoach.arch import load_registry`` —
    an explicit ``__init__.py`` re-export listed in ``__all__`` that
    pytest imported fine — and killed a green Developer step over a
    pre-existing import (SP-DEV-STEP-PREFLIGHT, 2026-07-04).
    """
    _seed_findings_package(tmp_path)
    _seed_module(
        tmp_path,
        "src/repoach/arch/registry.py",
        "def load_registry(root):\n    return root\n",
    )
    _seed_module(
        tmp_path,
        "src/repoach/arch/__init__.py",
        'from repoach.arch.registry import load_registry\n\n__all__ = ["load_registry"]\n',
    )
    candidate = _seed_module(
        tmp_path,
        "src/repoach/review/bridge.py",
        "from repoach.arch import load_registry\n",
    )
    ok, report = check_imports(tmp_path, [candidate])
    assert ok is True
    assert report == ""


def test_facade_aliased_and_plain_imports_count(tmp_path: Path) -> None:
    """``import x as y`` and plain ``import x`` aliases are facade names too."""
    _seed_findings_package(tmp_path)
    _seed_module(tmp_path, "src/repoach/arch/graph.py", "class Graph:\n    pass\n")
    _seed_module(
        tmp_path,
        "src/repoach/arch/__init__.py",
        "import json\nfrom repoach.arch.graph import Graph as ArchGraph\n",
    )
    candidate = _seed_module(
        tmp_path,
        "src/repoach/review/bridge.py",
        "from repoach.arch import ArchGraph, json\n",
    )
    ok, report = check_imports(tmp_path, [candidate])
    assert ok is True
    assert report == ""


def test_name_absent_from_the_facade_is_still_reported(tmp_path: Path) -> None:
    """The fix must not blanket-accept: unknown façade names still fail."""
    _seed_findings_package(tmp_path)
    _seed_module(
        tmp_path,
        "src/repoach/arch/registry.py",
        "def load_registry(root):\n    return root\n",
    )
    _seed_module(
        tmp_path,
        "src/repoach/arch/__init__.py",
        "from repoach.arch.registry import load_registry\n",
    )
    candidate = _seed_module(
        tmp_path,
        "src/repoach/review/bridge.py",
        "from repoach.arch import save_registry\n",
    )
    ok, report = check_imports(tmp_path, [candidate])
    assert ok is False
    assert "'save_registry' is not defined in 'repoach.arch'" in report


def test_try_guarded_name_is_accepted(tmp_path: Path) -> None:
    """Optional-dependency names defined inside try/except count as defined.

    The gate walked only the module's direct body, so the standard
    ``try: import tiktoken; ENCODER = ... except: ENCODER = None``
    pattern was invisible and a green Developer step importing ENCODER
    was refused (SP-USAGE-REASONING-SPLIT dispatch 2, 2026-07-04).
    """
    _seed_findings_package(tmp_path)
    _seed_module(
        tmp_path,
        "src/repoach/review/tokens.py",
        "try:\n    import tiktoken\n\n    ENCODER = tiktoken.get_encoding()\n"
        "except Exception:\n    ENCODER = None\n",
    )
    candidate = _seed_module(
        tmp_path,
        "src/repoach/review/bridge.py",
        "from repoach.review.tokens import ENCODER\n",
    )
    ok, report = check_imports(tmp_path, [candidate])
    assert ok is True
    assert report == ""


def test_if_guarded_name_is_accepted(tmp_path: Path) -> None:
    """Names assigned under a top-level if/else are defined either way."""
    _seed_findings_package(tmp_path)
    _seed_module(
        tmp_path,
        "src/repoach/review/flags.py",
        "import os\n\nif os.environ.get('X'):\n    MODE = 'x'\nelse:\n    MODE = 'y'\n",
    )
    candidate = _seed_module(
        tmp_path,
        "src/repoach/review/bridge.py",
        "from repoach.review.flags import MODE\n",
    )
    ok, report = check_imports(tmp_path, [candidate])
    assert ok is True
    assert report == ""
