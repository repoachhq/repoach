"""SP-GATE-REVERIFIES-AT-HEAD G6: a Vitest promise is judged by its own block.

tech_debt row 60: ``promised_body_non_trivial`` split every selector on
``::`` and parsed the file with Python's ``ast``. A selector written
``file.test.ts > describe > it`` was never a file (reported missing with
the test present), and the ``::`` spelling passed only because a
TypeScript ``SyntaxError`` counted as a non-empty body.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoach.review.plan import ActionPlan, PlanStep
from repoach.review.spec_gate import compute_spec_coverage, promised_body_non_trivial

_TEST_FILE = "frontend/src/InvoiceTable.test.ts"

_SOURCE = """import { describe, expect, it } from "vitest";
import { dueTotal } from "./InvoiceTable";

describe("dueTotal", () => {
  it("sums the unpaid invoices only", () => {
    expect(dueTotal([])).toBe("0.00");
  });
  it("is empty", () => {
    // nothing yet
  });
  it.todo("handles currencies");
  it("has no callback");
  test("is an expression", () => expect(dueTotal([])).toBe("0.00"));
  it("is a function", function () {
    expect(1).toBe(1);
  });
  it.skip("is skipped", () => {
    expect(1).toBe(2);
  });
  it("keeps a brace in a string", () => {
    expect("}").toBe("}");
  });
});
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    target = tmp_path / _TEST_FILE
    target.parent.mkdir(parents=True)
    target.write_text(_SOURCE, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "selector",
    [
        f"{_TEST_FILE} > dueTotal > sums the unpaid invoices only",
        f"{_TEST_FILE}::dueTotal::sums the unpaid invoices only",
        f"{_TEST_FILE} > is an expression",
        f"{_TEST_FILE} > is a function",
        f"{_TEST_FILE} > keeps a brace in a string",
        f"{_TEST_FILE} > dueTotal",
        _TEST_FILE,
    ],
)
def test_vitest_block_with_a_body_is_non_trivial(repo: Path, selector: str) -> None:
    assert promised_body_non_trivial(repo, selector) is True


@pytest.mark.parametrize(
    "name",
    ["is empty", "handles currencies", "has no callback", "is skipped", "is not there"],
)
@pytest.mark.parametrize("separator", [" > ", "::"])
def test_vitest_block_without_a_body_is_trivial(repo: Path, name: str, separator: str) -> None:
    assert promised_body_non_trivial(repo, f"{_TEST_FILE}{separator}{name}") is False


def test_typescript_parse_error_is_never_a_non_empty_body(tmp_path: Path) -> None:
    target = tmp_path / "frontend" / "src" / "helpers.ts"
    target.parent.mkdir(parents=True)
    target.write_text("export const x = (): number => 1;\n", encoding="utf-8")

    assert promised_body_non_trivial(tmp_path, "frontend/src/helpers.ts::x") is False


def test_coverage_counts_the_arrow_selector_present(repo: Path) -> None:
    plan = ActionPlan(
        spec_id="SP-X",
        title="due total",
        summary="due total",
        steps=[
            PlanStep(
                index=1,
                title="due total",
                action="add dueTotal",
                commit_message="feat: due total",
                done_when="vitest passes",
                files=[_TEST_FILE],
                unit_tests=[f"{_TEST_FILE} > dueTotal > sums the unpaid invoices only"],
            )
        ],
    )

    coverage = compute_spec_coverage(repo, spec_id="SP-X", plan=plan)

    assert coverage.missing == []
    assert coverage.covered is True
