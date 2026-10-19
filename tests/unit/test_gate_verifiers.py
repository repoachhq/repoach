"""Tests for SP-UNIVERSAL-GATES — the per-language verifier family and test selectors.

Each verifier is driven through :func:`run_verifiers` on a scratch
repository.  External tools are fake executables placed where the real
ones would be found (``node_modules/.bin`` for the TypeScript tools, a
``PATH`` directory for the command verifiers), so the plumbing —
binary resolution, argv, output parsing, ``file:line`` reporting — is
exercised without Node, shellcheck or Verilator installed.
"""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest

from repoach.gates.config import GatesConfig
from repoach.gates.selectors import (
    SelectorKind,
    is_js_test_file,
    parse_selector,
    run_selectors,
    selector_file,
    selector_present,
)
from repoach.gates.typescript import strip_jsonc
from repoach.gates.verifiers import VerifierStatus, run_verifiers
from repoach.review.plan import PlanStep
from repoach.review.spec_gate import promised_present


def _write(root: Path, rel: str, body: str) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def _exe(root: Path, rel: str, body: str) -> Path:
    target = _write(root, rel, "#!/bin/sh\n" + body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def _config(*languages: str, **gates: object) -> GatesConfig:
    return GatesConfig.from_mapping({"project": {"languages": list(languages)}, "gates": gates})


@pytest.fixture
def bare_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``PATH`` holding only a fresh directory (no real toolchain)."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def _ts_project(root: Path) -> None:
    _write(root, "frontend/package.json", '{"name": "web"}\n')
    _write(
        root,
        "frontend/tsconfig.json",
        '{\n  "files": [],\n  "references": [{ "path": "./tsconfig.app.json" }]\n}\n',
    )
    _write(
        root,
        "frontend/tsconfig.app.json",
        "{\n  /* Path aliases */\n"
        '  "compilerOptions": {\n    "baseUrl": ".",\n    "paths": { "@/*": ["./src/*"] },\n  },\n'
        '  "include": ["src"]\n}\n',
    )


def _outcome(report, verifier: str):
    return next(o for o in report.outcomes if o.verifier == verifier)


def test_strip_jsonc_keeps_glob_strings() -> None:
    text = '{\n  // c\n  "paths": {"@/*": ["./src/*"]}, /* x */\n}'
    assert json.loads(strip_jsonc(text)) == {"paths": {"@/*": ["./src/*"]}}


def test_tsc_error_is_red_with_file_and_line(tmp_path: Path, bare_path: Path) -> None:
    _ts_project(tmp_path)
    _write(tmp_path, "frontend/src/bad.ts", "export const n: number = 'x';\n")
    _exe(
        tmp_path,
        "frontend/node_modules/.bin/tsc",
        "echo \"src/bad.ts(1,14): error TS2322: Type 'string' is not assignable to type 'number'.\"\n"
        'echo "args=$*" >&2\nexit 2\n',
    )
    report = run_verifiers(tmp_path, ["frontend/src/bad.ts"], _config("typescript"))
    tsc = _outcome(report, "tsc")
    assert tsc.status is VerifierStatus.FAIL
    assert tsc.detail.splitlines()[0] == (
        "frontend/src/bad.ts:1:14: error TS2322: Type 'string' is not assignable to type 'number'."
    )
    assert report.passed is False


def test_tsc_error_in_an_untouched_file_does_not_block(tmp_path: Path, bare_path: Path) -> None:
    _ts_project(tmp_path)
    _write(tmp_path, "frontend/src/ok.ts", "/** Doc. */\nexport const n = 1;\n")
    _write(tmp_path, "frontend/src/legacy.ts", "export const m: number = 'x';\n")
    _exe(
        tmp_path,
        "frontend/node_modules/.bin/tsc",
        "echo \"src/legacy.ts(1,14): error TS2322: Type 'string' is not assignable.\"\nexit 2\n",
    )
    report = run_verifiers(tmp_path, ["frontend/src/ok.ts"], _config("typescript"))
    tsc = _outcome(report, "tsc")
    assert tsc.status is VerifierStatus.ADVISORY
    assert "frontend/src/legacy.ts:1:14" in tsc.detail
    assert report.passed is True


def test_tsc_green_and_targets_the_referenced_project(tmp_path: Path, bare_path: Path) -> None:
    _ts_project(tmp_path)
    _write(tmp_path, "frontend/src/ok.ts", "/** Doc. */\nexport const n = 1;\n")
    _exe(tmp_path, "frontend/node_modules/.bin/tsc", 'echo "$*" > tsc-args.txt\nexit 0\n')
    report = run_verifiers(tmp_path, ["frontend/src/ok.ts"], _config("typescript"))
    assert _outcome(report, "tsc").status is VerifierStatus.PASS
    assert (tmp_path / "frontend/tsc-args.txt").read_text(encoding="utf-8").strip() == (
        "--noEmit -p tsconfig.app.json"
    )


def test_tsc_missing_is_not_applicable(tmp_path: Path, bare_path: Path) -> None:
    _ts_project(tmp_path)
    _write(tmp_path, "frontend/src/ok.ts", "export const n = 1;\n")
    report = run_verifiers(tmp_path, ["frontend/src/ok.ts"], _config("typescript"))
    tsc = _outcome(report, "tsc")
    assert (tsc.status, tsc.detail) == (VerifierStatus.NOT_APPLICABLE, "binary not found: tsc")


def test_eslint_error_is_red(tmp_path: Path, bare_path: Path) -> None:
    _ts_project(tmp_path)
    _write(tmp_path, "frontend/eslint.config.js", "export default [];\n")
    source = _write(tmp_path, "frontend/src/a.ts", "const unused = 1;\n")
    payload = json.dumps(
        [
            {
                "filePath": str(source),
                "messages": [
                    {
                        "line": 1,
                        "column": 7,
                        "severity": 2,
                        "ruleId": "no-unused-vars",
                        "message": "'unused' is assigned a value but never used.",
                    }
                ],
            }
        ]
    )
    _write(tmp_path, "frontend/report.json", payload)
    _exe(
        tmp_path,
        "frontend/node_modules/.bin/eslint",
        "IFS= read -r line < report.json\nprintf '%s\\n' \"$line\"\nexit 1\n",
    )
    report = run_verifiers(tmp_path, ["frontend/src/a.ts"], _config("typescript"))
    eslint = _outcome(report, "eslint")
    assert eslint.status is VerifierStatus.FAIL
    assert eslint.detail == (
        "frontend/src/a.ts:1:7: no-unused-vars: 'unused' is assigned a value but never used."
    )


def test_ts_imports_resolve_through_tsconfig_paths(tmp_path: Path, bare_path: Path) -> None:
    _ts_project(tmp_path)
    _write(tmp_path, "frontend/src/lib/api.ts", "/** Api. */\nexport const api = 1;\n")
    _write(
        tmp_path,
        "frontend/src/page.tsx",
        "import { api } from '@/lib/api';\nimport React from 'react';\n"
        "import { gone } from './missing';\n/** Page. */\nexport const Page = api;\n",
    )
    report = run_verifiers(tmp_path, ["frontend/src/page.tsx"], _config("typescript"))
    imports = _outcome(report, "ts_imports")
    assert imports.status is VerifierStatus.FAIL
    assert imports.detail == "frontend/src/page.tsx:3: cannot resolve import './missing'"


def test_jsdoc_is_advisory_never_blocking(tmp_path: Path, bare_path: Path) -> None:
    _write(tmp_path, "web/util.ts", "export function add(a: number) {\n  return a;\n}\n")
    report = run_verifiers(tmp_path, ["web/util.ts"], _config("typescript"))
    jsdoc = _outcome(report, "jsdoc")
    assert jsdoc.status is VerifierStatus.ADVISORY
    assert jsdoc.detail == "web/util.ts:1: export 'add' has no JSDoc"
    assert _outcome(report, "tsc").status is VerifierStatus.NOT_APPLICABLE


def test_shellcheck_finding_is_red(tmp_path: Path, bare_path: Path) -> None:
    _write(tmp_path, "scripts/run.sh", "#!/bin/sh\n[ $x == y ]\n")
    _exe(
        bare_path.parent,
        "fake-bin/shellcheck",
        'echo "$3:2:3: warning: x is referenced but not assigned. [SC2154]"\nexit 1\n',
    )
    _exe(bare_path.parent, "fake-bin/bash", "exit 0\n")
    report = run_verifiers(tmp_path, ["scripts/run.sh"], _config("shell"))
    shellcheck = _outcome(report, "shellcheck")
    assert shellcheck.status is VerifierStatus.FAIL
    assert "scripts/run.sh:2:3: warning: x is referenced but not assigned. [SC2154]" in (
        shellcheck.detail
    )
    assert _outcome(report, "bash_n").status is VerifierStatus.PASS


def test_bash_syntax_error_in_an_extensionless_hook_is_red(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    _write(tmp_path, ".githooks/pre-commit", "#!/usr/bin/env bash\nif then fi\n")
    report = run_verifiers(
        tmp_path,
        [".githooks/pre-commit"],
        _config("shell", disabled_verifiers=["shellcheck"]),
    )
    assert _outcome(report, "bash_n").status is VerifierStatus.FAIL
    assert _outcome(report, "shellcheck").status is VerifierStatus.NOT_APPLICABLE


def test_verilog_without_verilator_names_the_binary(tmp_path: Path, bare_path: Path) -> None:
    _write(tmp_path, "rtl/top.v", "module top; endmodule\n")
    report = run_verifiers(tmp_path, ["rtl/top.v"], _config("verilog"))
    verilator = _outcome(report, "verilator")
    assert (verilator.status, verilator.detail) == (
        VerifierStatus.NOT_APPLICABLE,
        "binary not found: verilator",
    )
    assert report.passed is True


def test_verilator_and_gcc_findings_are_red(tmp_path: Path, bare_path: Path) -> None:
    _write(tmp_path, "rtl/top.v", "module top; endmodule\n")
    _write(tmp_path, "fw/main.c", "int main( {\n")
    _exe(bare_path.parent, "fake-bin/verilator", 'echo "%Warning-UNUSED: $3:1:8: unused"\nexit 1\n')
    _exe(bare_path.parent, "fake-bin/gcc", "echo \"$2:1:10: error: expected ')'\" >&2\nexit 1\n")
    advisory = run_verifiers(tmp_path, ["rtl/top.v", "fw/main.c"], _config("verilog", "c"))
    assert _outcome(advisory, "verilator").status is VerifierStatus.ADVISORY
    assert _outcome(advisory, "gcc").status is VerifierStatus.ADVISORY
    assert advisory.passed is True
    declared = _config(
        "verilog",
        "c",
        verifier_commands={
            "verilator": "verilator --lint-only -Wall {file} -Igen",
            "gcc": "gcc -fsyntax-only {file} -Iinclude",
        },
    )
    report = run_verifiers(tmp_path, ["rtl/top.v", "fw/main.c"], declared)
    assert _outcome(report, "verilator").status is VerifierStatus.FAIL
    assert "rtl/top.v:1:8: unused" in _outcome(report, "verilator").detail
    assert "fw/main.c:1:10: error: expected ')'" in _outcome(report, "gcc").detail
    assert report.passed is False


def test_generated_hls4ml_module_does_not_red_the_gate(tmp_path: Path) -> None:
    if shutil.which("verilator") is None:
        pytest.skip("verilator not installed")
    _write(
        tmp_path,
        "soc/nn_accelerator_wb.v",
        "module nn_accelerator_wb(input wire clk);\n  myproject accel (.ap_clk(clk));\nendmodule\n",
    )
    report = run_verifiers(tmp_path, ["soc/nn_accelerator_wb.v"], _config("verilog"))
    verilator = _outcome(report, "verilator")
    assert verilator.status is VerifierStatus.ADVISORY
    assert "myproject" in verilator.detail
    assert report.passed is True


def test_verifier_command_override_and_advisory_policy(tmp_path: Path, bare_path: Path) -> None:
    _write(tmp_path, "rtl/top.v", "module top; endmodule\n")
    _exe(bare_path.parent, "fake-bin/lint-v", 'echo "checked $1"\nexit 1\n')
    config = _config(
        "verilog",
        verifier_commands={"verilator": "lint-v {file}"},
        advisory_verifiers=["verilator"],
    )
    report = run_verifiers(tmp_path, ["rtl/top.v"], config)
    verilator = _outcome(report, "verilator")
    assert verilator.status is VerifierStatus.ADVISORY
    assert "checked rtl/top.v" in verilator.detail
    assert report.passed is True


def test_yaml_parse_error_is_red_and_custom_tags_parse(tmp_path: Path) -> None:
    _write(tmp_path, ".gitlab-ci.yml", "test:\n  script:\n    - !reference [.base, script]\n")
    _write(tmp_path, "bad.yml", "a: [1, 2\nb: 3\n")
    report = run_verifiers(tmp_path, [".gitlab-ci.yml", "bad.yml"], _config("yaml"))
    yaml_outcome = _outcome(report, "yaml_parse")
    assert yaml_outcome.status is VerifierStatus.FAIL
    assert yaml_outcome.detail.startswith("bad.yml:")
    assert ".gitlab-ci.yml" not in yaml_outcome.detail


def test_markdown_broken_link_is_red(tmp_path: Path) -> None:
    _write(tmp_path, "docs/guide.md", "x\n")
    _write(
        tmp_path,
        "README.md",
        "See [guide](docs/guide.md#top), [site](https://example.com), [gone](docs/nope.md).\n"
        "```\n[ignored](missing.md)\n```\n",
    )
    report = run_verifiers(tmp_path, ["README.md"], _config("markdown"))
    links = _outcome(report, "md_links")
    assert links.status is VerifierStatus.FAIL
    assert links.detail == "README.md:1: broken link: docs/nope.md"


def test_go_has_no_verifier_and_says_so(tmp_path: Path) -> None:
    _write(tmp_path, "main.go", "package main\n")
    report = run_verifiers(tmp_path, ["main.go"], _config("go"))
    assert [(o.status, o.detail) for o in report.outcomes] == [
        (VerifierStatus.NOT_APPLICABLE, "no verifier for go")
    ]


@pytest.mark.parametrize(
    ("raw", "kind", "file", "name"),
    [
        (
            "tests/unit/test_x.py::TestA::test_b",
            SelectorKind.PYTEST,
            "tests/unit/test_x.py",
            "test_b",
        ),
        (
            "frontend/src/x.test.ts > adds > two",
            SelectorKind.VITEST,
            "frontend/src/x.test.ts",
            "two",
        ),
        ("src/__tests__/util.ts::parses", SelectorKind.VITEST, "src/__tests__/util.ts", "parses"),
        ("./pkg/store -run TestPut", SelectorKind.GO, "./pkg/store", "TestPut"),
        ("cmd:make check", SelectorKind.COMMAND, "", ""),
    ],
)
def test_parse_selector_forms(raw: str, kind: SelectorKind, file: str, name: str) -> None:
    parsed = parse_selector(raw)
    assert parsed is not None
    assert (parsed.kind, parsed.file, parsed.name) == (kind, file, name)


@pytest.mark.parametrize("raw", ["src/x.ts > name", "just words", "-k foo", "cmd:", "a.py > b"])
def test_unknown_selector_forms_parse_to_none(raw: str) -> None:
    assert parse_selector(raw) is None


def test_js_test_file_shapes() -> None:
    assert is_js_test_file("src/a.test.tsx") and is_js_test_file("src/a.spec.js")
    assert is_js_test_file("src/__tests__/a.ts")
    assert not is_js_test_file("src/a.ts")
    assert selector_file("frontend/src/x.test.ts > adds") == "frontend/src/x.test.ts"
    assert selector_file("tests/x.py::test_y") == "tests/x.py"


def test_vitest_selector_is_present_and_runs(tmp_path: Path, bare_path: Path) -> None:
    _write(tmp_path, "frontend/package.json", "{}\n")
    _write(
        tmp_path,
        "frontend/src/x.test.ts",
        "describe('math', () => {\n  it(\"adds two numbers\", () => {});\n});\n",
    )
    _exe(tmp_path, "frontend/node_modules/.bin/vitest", 'echo "vitest $*"\nexit 0\n')
    selector = "frontend/src/x.test.ts > math > adds two numbers"
    parsed = parse_selector(selector)
    assert parsed is not None
    assert selector_present(tmp_path, parsed) is True
    assert promised_present(tmp_path, selector) is True
    assert promised_present(tmp_path, "frontend/src/x.test.ts > never written") is False
    ok, tail = run_selectors(tmp_path, [selector], _config("typescript"))
    assert ok is True
    assert "vitest run src/x.test.ts -t math adds two numbers" in tail
    unknown_ok, unknown_tail = run_selectors(tmp_path, ["just words"], _config("typescript"))
    assert unknown_ok is False and unknown_tail.startswith("not applicable")


def test_plan_step_accepts_vitest_and_refuses_a_bare_test_file() -> None:
    step = PlanStep(
        index=1,
        title="widget",
        action="add the widget",
        files=["frontend/src/w.ts", "frontend/src/w.test.ts"],
        commit_message="feat(web): add the widget",
        done_when="the widget test is green",
        unit_tests=["frontend/src/w.test.ts > renders the widget"],
    )
    assert step.unit_tests == ["frontend/src/w.test.ts > renders the widget"]
    with pytest.raises(ValueError, match=r"file\.test\.ts > test name"):
        PlanStep(
            index=1,
            title="widget",
            action="add the widget",
            files=["frontend/src/w.ts", "frontend/src/w.test.ts"],
            commit_message="feat(web): add the widget",
            done_when="the widget test is green",
            unit_tests=["frontend/src/w.test.ts"],
        )
