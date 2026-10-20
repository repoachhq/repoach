"""Tests for SP-UNIVERSAL-GATES — language detection and language-gated Python verifiers.

AC1: a ``missing_docstring`` finding on a ``.ts`` file ends
``NOT_APPLICABLE`` with method ``language`` whatever languages are
declared; a ``.py`` file under a declared root still runs ``ast``.
Also pinned: the mixed Python + TypeScript diff (Python verifiers on
``.py`` only, ``NOT_APPLICABLE`` for ``.ts``), the built-in
configuration (no ``repoach.toml``) behaving as before, the merge gate
treating ``not_applicable`` as settled, the Coder jail reading the
project's ``[coder]`` table (AC5), the edge-honesty gate on a repo with
no governed spec (AC3) and with frontmatter opt-in (AC4), and
``repoach doctor --gates``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoach.cli.main import app
from repoach.gates.config import (
    GatesConfig,
    GatesConfigError,
    applicable,
    load_gates_config,
    python_packages,
)
from repoach.gates.languages import Language, diff_languages, language_of
from repoach.gates.verifiers import VerifierStatus, run_verifiers
from repoach.review.coder_loop import is_path_allowed, run_ruff_gate
from repoach.review.finding_verifiers import verify_finding, verify_findings_for_pr
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from repoach.review.import_gate import check_imports
from repoach.review.merge_gate import gather_merge_facts, summarise_ledger_facts


def _finding(claim_type: ClaimType, file: str) -> Finding:
    return Finding(
        pr_number=1,
        head_sha="abc1234",
        round=1,
        finder="scribe",
        claim_type=claim_type,
        severity="blocking",
        file=file,
        line_start=1,
        line_end=1,
        claim="public symbol lacks a docstring",
        evidence_pointer=f"{file}:1",
    )


def _config(*languages: str, src_roots: tuple[str, ...] = ("src", "frontend/src")) -> GatesConfig:
    return GatesConfig.from_mapping(
        {
            "project": {
                "languages": list(languages),
                "src_roots": list(src_roots),
                "test_roots": ["tests"],
            }
        }
    )


def _write(root: Path, rel: str, body: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("src/a.py", Language.PYTHON),
        ("frontend/src/x.ts", Language.TYPESCRIPT),
        ("frontend/src/X.tsx", Language.TYPESCRIPT),
        ("web/app.js", Language.JAVASCRIPT),
        ("cmd/main.go", Language.GO),
        ("crates/a/lib.rs", Language.RUST),
        ("scripts/lint.sh", Language.SHELL),
        (".gitlab-ci.yml", Language.YAML),
        ("docs/README.md", Language.MARKDOWN),
        ("rtl/top.sv", Language.VERILOG),
        ("fw/main.c", Language.C),
        ("assets/logo.png", Language.OTHER),
    ],
)
def test_language_of_by_extension(path: str, language: Language) -> None:
    assert language_of(path) is language


def test_language_of_reads_the_shebang_of_an_extensionless_hook(tmp_path: Path) -> None:
    _write(tmp_path, "scripts/hooks/pre-push", "#!/usr/bin/env bash\nexit 0\n")
    _write(tmp_path, "tools/run", "#!/usr/bin/python3.11\nprint(1)\n")
    assert language_of("scripts/hooks/pre-push", tmp_path) is Language.SHELL
    assert language_of("tools/run", tmp_path) is Language.PYTHON
    assert diff_languages(["a.py", "b.ts"]) == frozenset({Language.PYTHON, Language.TYPESCRIPT})


def test_missing_docstring_on_ts_not_applicable_with_typescript_only(tmp_path: Path) -> None:
    _write(tmp_path, "frontend/src/x.ts", "export const x = 1;\n")
    status, method, result = verify_finding(
        _finding(ClaimType.MISSING_DOCSTRING, "frontend/src/x.ts"),
        repo_root=tmp_path,
        config=_config("typescript"),
    )
    assert status is FindingStatus.NOT_APPLICABLE
    assert method == "language"
    assert result == "not python: .ts"


def test_missing_docstring_on_ts_not_applicable_with_python_declared(tmp_path: Path) -> None:
    _write(tmp_path, "frontend/src/x.ts", "export const x = 1;\n")
    status, method, result = verify_finding(
        _finding(ClaimType.MISSING_DOCSTRING, "frontend/src/x.ts"),
        repo_root=tmp_path,
        config=_config("python", "typescript"),
    )
    assert status is FindingStatus.NOT_APPLICABLE
    assert method == "language"
    assert result == "not python: .ts"


def test_missing_docstring_on_py_under_src_runs_ast(tmp_path: Path) -> None:
    _write(tmp_path, "src/x.py", "def public():\n    return 1\n")
    status, method, _ = verify_finding(
        _finding(ClaimType.MISSING_DOCSTRING, "src/x.py"),
        repo_root=tmp_path,
        config=_config("python", "typescript"),
    )
    assert status is FindingStatus.VERIFIED
    assert method == "ast_docstring"


def test_py_outside_src_roots_is_not_applicable(tmp_path: Path) -> None:
    _write(tmp_path, "ci/autofix/fix.py", "def public():\n    return 1\n")
    status, method, result = verify_finding(
        _finding(ClaimType.MISSING_DOCSTRING, "ci/autofix/fix.py"),
        repo_root=tmp_path,
        config=_config("python"),
    )
    assert (status, method, result) == (
        FindingStatus.NOT_APPLICABLE,
        "language",
        "outside src_roots",
    )


def test_py_when_python_not_declared_is_not_applicable(tmp_path: Path) -> None:
    _write(tmp_path, "src/x.py", "def public():\n    return 1\n")
    status, _, result = verify_finding(
        _finding(ClaimType.LINT_CONVENTION, "src/x.py"),
        repo_root=tmp_path,
        config=_config("typescript"),
    )
    assert status is FindingStatus.NOT_APPLICABLE
    assert result == "python not declared"


def test_lint_convention_on_tsx_never_parses_it(tmp_path: Path) -> None:
    _write(tmp_path, "frontend/src/X.tsx", "export const X = () => <div/>; // comment\n")
    status, method, result = verify_finding(
        _finding(ClaimType.LINT_CONVENTION, "frontend/src/X.tsx"),
        repo_root=tmp_path,
        config=_config("python", "typescript"),
    )
    assert (status, method, result) == (
        FindingStatus.NOT_APPLICABLE,
        "language",
        "not python: .tsx",
    )


def test_builtin_config_keeps_python_verifiers_everywhere(tmp_path: Path) -> None:
    config = load_gates_config(tmp_path)
    assert config.builtin
    assert config.lint_cmd is None and config.test_cmd is None
    assert applicable("scripts/tool.py", config) == (True, "")
    _write(tmp_path, "scripts/tool.py", "def public():\n    return 1\n")
    status, method, _ = verify_finding(
        _finding(ClaimType.MISSING_DOCSTRING, "scripts/tool.py"), repo_root=tmp_path
    )
    assert (status, method) == (FindingStatus.REFUTED, "ast_docstring")


def test_docstring_exempt_defaults_to_test_roots_on_a_foreign_repo(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_x.py", "def helper():\n    return 1\n")
    status, _, result = verify_finding(
        _finding(ClaimType.MISSING_DOCSTRING, "tests/test_x.py"),
        repo_root=tmp_path,
        config=_config("python"),
    )
    assert status is FindingStatus.REFUTED
    assert "docstring-exempt" in result


def test_mixed_diff_runs_python_on_py_only(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _write(tmp_path, "src/pkg/__init__.py", "")
    _write(
        tmp_path,
        "src/pkg/a.py",
        '"""Doc."""\n\nfrom pkg import missing_mod\n\n\ndef f():\n    return 1\n',
    )
    _write(tmp_path, "frontend/src/x.ts", "import { y } from './nowhere';\nexport const x = y;\n")
    _write(
        tmp_path,
        "repoach.toml",
        '[project]\nlanguages = ["python", "typescript"]\nsrc_roots = ["src", "frontend/src"]\n',
    )
    record_finding(db, _finding(ClaimType.MISSING_DOCSTRING, "src/pkg/a.py"))
    record_finding(db, _finding(ClaimType.MISSING_DOCSTRING, "frontend/src/x.ts"))

    counts = verify_findings_for_pr(db, pr_number=1, repo_root=tmp_path, head_sha="h1")

    assert counts == {"verified": 1, "refuted": 0, "deferred": 0, "not_applicable": 1}
    rows = {f.file: f for f in fetch_findings(db, 1)}
    assert rows["src/pkg/a.py"].status is FindingStatus.VERIFIED
    assert rows["frontend/src/x.ts"].status is FindingStatus.NOT_APPLICABLE
    assert rows["frontend/src/x.ts"].verification_method == "language"
    assert rows["frontend/src/x.ts"].verification_result == "not python: .ts"

    ok, report = check_imports(tmp_path, ["src/pkg/a.py", "frontend/src/x.ts"])
    assert not ok
    assert "src/pkg/a.py" in report and "frontend/src/x.ts" not in report

    facts = summarise_ledger_facts(db, pr_number=1, head_sha="h1")
    assert facts.open_blocking_findings == 1


def test_not_applicable_is_settled_by_the_merge_gate(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _write(tmp_path, "repoach.toml", '[project]\nlanguages = ["typescript"]\n')
    record_finding(db, _finding(ClaimType.MISSING_DOCSTRING, "frontend/src/x.ts"))
    verify_findings_for_pr(db, pr_number=1, repo_root=tmp_path, head_sha="h1")
    facts = summarise_ledger_facts(db, pr_number=1, head_sha="h1")
    assert facts.open_blocking_findings == 0


def test_run_verifiers_reports_undeclared_language_not_applicable(tmp_path: Path) -> None:
    _write(tmp_path, "frontend/src/x.ts", "export const x = 1;\n")
    _write(tmp_path, "src/a.py", "x = 1\n")
    report = run_verifiers(tmp_path, ["src/a.py", "frontend/src/x.ts"], _config("python"))
    assert report.passed
    assert [(o.verifier, o.status, o.detail) for o in report.outcomes] == [
        ("language", VerifierStatus.NOT_APPLICABLE, "language not declared: typescript")
    ]


def test_python_packages_from_declared_src_roots(tmp_path: Path) -> None:
    _write(tmp_path, "src/yoob/__init__.py", "")
    _write(tmp_path, "api/__init__.py", "")
    config = _config("python", src_roots=("src", "api", "frontend/src"))
    packages = python_packages(tmp_path, config)
    assert packages == {"yoob": (tmp_path / "src").resolve(), "api": tmp_path.resolve()}
    assert python_packages(tmp_path, GatesConfig.builtin_default()) == {
        "repoach": (tmp_path / "src").resolve()
    }


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return completed.stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    return root


def test_coder_jail_reads_the_project_coder_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _write(
        root,
        "repoach.toml",
        '[project]\nlanguages = ["python", "typescript"]\n'
        '[coder]\nforbidden_prefixes = ["ci/", "scripts/hooks/"]\n',
    )
    monkeypatch.chdir(root)
    assert [
        is_path_allowed(p)
        for p in (".gitlab-ci.yml", "ci/x.sh", ".claude/settings.json", "src/ok.py")
    ] == [False, False, False, True]
    assert is_path_allowed("scripts/hooks/pre-push") is False
    assert is_path_allowed("repoach.toml") is False
    assert is_path_allowed(".gemini/settings.json") is False
    assert is_path_allowed("frontend/src/x.ts") is True


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def test_arch_check_on_a_repo_with_zero_governed_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _write(root, "main.go", "package main\n")
    _commit_all(root, "init")
    _git(root, "checkout", "-q", "-b", "feat/x")
    _write(root, "repoach.toml", '[project]\nlanguages = ["go"]\n')
    _write(root, "docs/specs/_TEMPLATE.md", "---\nid: SP-X\n---\n")
    _write(root, "docs/specs/2026-10-19_SP-FOO_foo.md", "# a spec without frontmatter\n")
    _commit_all(root, "add config")
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["arch", "check", "--base", "main"])
    assert result.exit_code == 0, result.output
    assert "edge-honesty: ok (0 governed specs)" in result.output


def test_arch_check_require_frontmatter_keeps_the_presence_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _write(root, "README.md", "x\n")
    _commit_all(root, "init")
    _git(root, "checkout", "-q", "-b", "feat/x")
    _write(root, "repoach.toml", "[arch]\nrequire_frontmatter = true\n")
    _write(root, "docs/specs/2026-10-19_SP-FOO_foo.md", "# a spec without frontmatter\n")
    _commit_all(root, "add spec")
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["arch", "check", "--base", "main"])
    assert result.exit_code == 1
    assert "ungoverned new spec: docs/specs/2026-10-19_SP-FOO_foo.md" in result.output


def test_doctor_gates_prints_the_language_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _write(
        root,
        "repoach.toml",
        '[project]\nlanguages = ["python", "typescript", "verilog"]\n'
        '[commands]\nlint = "./scripts/lint.sh"\n',
    )
    monkeypatch.chdir(root)
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    git = shutil.which("git")
    assert git is not None
    (only_git / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(only_git))
    result = CliRunner().invoke(app, ["doctor", "--gates"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "commands.lint=./scripts/lint.sh" in lines
    assert "commands.test=(none: skipped)" in lines
    typescript = next(line for line in lines if line.startswith("typescript "))
    assert " yes " in typescript and "tsc=missing: tsc" in typescript
    verilog = next(line for line in lines if line.startswith("verilog "))
    assert "verilator=missing: verilator" in verilog
    rust = next(line for line in lines if line.startswith("rust "))
    assert " no " in rust and "(none)" in rust


_BAD_TOML = '[project]\nlanguages = ["cobol"]\n'


@pytest.mark.parametrize(
    "argv",
    [
        ["arch", "check", "--base", "main"],
        ["review", "gate", "1"],
        ["review", "fix", "1"],
        ["review", "merge", "1"],
    ],
)
def test_bad_repoach_toml_is_a_named_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    root = _repo(tmp_path)
    _write(root, "README.md", "x\n")
    _commit_all(root, "init")
    _write(root, "repoach.toml", _BAD_TOML)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "repoach.toml: key 'project.languages': unknown language 'cobol'" in result.output
    assert "Traceback" not in result.output


def test_bad_repoach_toml_raises_a_named_error_in_process(tmp_path: Path) -> None:
    _write(tmp_path, "repoach.toml", _BAD_TOML)
    with pytest.raises(GatesConfigError, match=r"repoach\.toml: key 'project\.languages': unknown"):
        load_gates_config(tmp_path)
    with pytest.raises(GatesConfigError):
        run_ruff_gate(tmp_path)
    db = tmp_path / "f.db"
    with pytest.raises(GatesConfigError):
        gather_merge_facts(db, pr_number=1, repo_root=tmp_path, head_sha="h", ci_green=True)


def test_foreign_repo_without_config_does_not_require_frontmatter(tmp_path: Path) -> None:
    assert load_gates_config(tmp_path).require_frontmatter is False
    _write(tmp_path, "pyproject.toml", '[project]\nname = "repoach"\n')
    assert load_gates_config(tmp_path).require_frontmatter is True
