#!/usr/bin/env python3
"""Render ``docs/comparison.md`` from ``docs/comparison.yaml`` and check every source.

SP-COMPARISON-PAGE. The YAML is the source: one entry per product, each
cell a ``{value, source_url, checked_on}`` typed after reading the
page, or ``{value: n/m}`` when the page says nothing (not measured: the
page cannot say). Nothing is scraped; this script only proves that each
source still answers and that no reading is older than
``constraints.source_max_age_days``.

Usage::

    python scripts/gen_comparison.py               # render, check, write when green
    python scripts/gen_comparison.py --offline     # render and write, check nothing
    python scripts/gen_comparison.py --check-only  # check, write nothing

Exit codes:

* ``0`` every source answers and every reading is fresh; the page is written.
* ``1`` a dead source, one ``dead: <url> (comparison.yaml: <product>.<cell>)``
  line each (a link of the page outside the YAML is named
  ``<file>:<line> <url> <status>``).
* ``2`` the YAML is invalid, or no URL got any HTTP answer (``cannot say``);
  the page is not rewritten.
* ``3`` a reading is stale: ``stale: N cell(s) older than D days``.

The links of the rendered page are judged by ``scripts/check_links.py``,
so the page and the README share one notion of a dead link.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import check_links
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_YAML = REPO_ROOT / "docs" / "comparison.yaml"
DEFAULT_OUT = REPO_ROOT / "docs" / "comparison.md"
NOT_MEASURED = "n/m"
ISSUES_URL = "https://github.com/repoachhq/repoach/issues"

EXIT_OK = 0
EXIT_DEAD = 1
EXIT_INVALID = 2
EXIT_STALE = 3

COLUMNS: dict[str, str] = {
    "loop": "Loop",
    "forges": "Forges",
    "blocks_merge_on_evidence": "Blocks a merge on evidence",
    "reverifies_at_merge_head": "Re-verifies findings at the merge head",
    "spec_gates_plan": "Spec in the repo gates the plan",
    "model_choice": "Model choice (BYOK)",
    "price": "Price as listed",
    "self_host": "Self-host",
}


class Cell(BaseModel):
    """One sourced cell, or ``n/m``.

    Attributes:
        value: The text shown in the table.
        source_url: The page the value was read on; absent for ``n/m``.
        checked_on: The day the page was read; absent for ``n/m``.
    """

    model_config = ConfigDict(extra="forbid")

    value: str
    source_url: str | None = None
    checked_on: date | None = None

    @model_validator(mode="after")
    def _sourced_unless_not_measured(self) -> Cell:
        """Require a source and a date on every cell but ``n/m``."""
        if self.value == NOT_MEASURED:
            if self.source_url or self.checked_on:
                raise ValueError("an n/m cell carries no source")
            return self
        if not self.source_url or self.checked_on is None:
            raise ValueError(f"cell {self.value!r} needs source_url and checked_on")
        return self

    @property
    def measured(self) -> bool:
        """Whether the cell carries a sourced value."""
        return self.value != NOT_MEASURED


class Product(BaseModel):
    """One row of the table.

    Attributes:
        name: The product name.
        vendor_url: The product page the name links to.
        loop: Where it sits in the change lifecycle.
        forges: The code hosts it serves.
        blocks_merge_on_evidence: Whether it refuses a merge on verified findings.
        reverifies_at_merge_head: Whether findings are re-checked at the head merged.
        spec_gates_plan: Whether a spec committed in the repository gates the plan.
        model_choice: Who picks the model, and whether your key works.
        price: The list price as published.
        self_host: Whether it runs on your infrastructure.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    vendor_url: str
    loop: Cell
    forges: Cell
    blocks_merge_on_evidence: Cell
    reverifies_at_merge_head: Cell
    spec_gates_plan: Cell
    model_choice: Cell
    price: Cell
    self_host: Cell

    def cell(self, key: str) -> Cell:
        """Return the cell of column *key*."""
        value = getattr(self, key)
        assert isinstance(value, Cell)
        return value


class PrSource(BaseModel):
    """A tool that opens PRs repoach then reviews; listed, never compared.

    Attributes:
        name: The tool.
        line: One line on where it sits relative to repoach.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    line: str


class Retired(BaseModel):
    """A product no longer offered.

    Attributes:
        name: The product.
        last_checked_on: The last day its row was read.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    last_checked_on: date


class Constraints(BaseModel):
    """The freshness rule.

    Attributes:
        source_max_age_days: How old a reading may be before it is stale.
    """

    model_config = ConfigDict(extra="forbid")

    source_max_age_days: int = 90


class Comparison(BaseModel):
    """The whole YAML document.

    Attributes:
        constraints: The freshness rule.
        products: The table rows, repoach first.
        pr_sources: Tools that open PRs, listed under their own heading.
        retired: Products moved out of the table.
    """

    model_config = ConfigDict(extra="forbid")

    constraints: Constraints = Constraints()
    products: list[Product]
    pr_sources: list[PrSource] = []
    retired: list[Retired] = []


def load(path: Path) -> Comparison:
    """Parse and validate the YAML.

    Args:
        path: The YAML file.

    Returns:
        The validated document.

    Raises:
        ValueError: The file is not valid YAML or breaks the schema.
    """
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc
    try:
        return Comparison.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc


def _escape(text: str) -> str:
    """Make *text* safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def oldest_reading(comparison: Comparison) -> date | None:
    """Return the oldest ``checked_on`` of the table, ``None`` when nothing is sourced."""
    days = [
        product.cell(key).checked_on
        for product in comparison.products
        for key in COLUMNS
        if product.cell(key).checked_on is not None
    ]
    return min((d for d in days if d is not None), default=None)


def render(comparison: Comparison) -> str:
    """Render the Markdown page.

    Args:
        comparison: The validated document.

    Returns:
        The page, ending with a newline.
    """
    oldest = oldest_reading(comparison)
    footnotes: dict[tuple[str, date], int] = {}
    rows: list[str] = []
    for product in comparison.products:
        cells = [f"[{_escape(product.name)}]({product.vendor_url})"]
        for key in COLUMNS:
            cell = product.cell(key)
            if not cell.measured or cell.source_url is None or cell.checked_on is None:
                cells.append(NOT_MEASURED)
                continue
            ref = (cell.source_url, cell.checked_on)
            number = footnotes.setdefault(ref, len(footnotes) + 1)
            cells.append(f"{_escape(cell.value)}[^{number}]")
        rows.append("| " + " | ".join(cells) + " |")
    header = "| Product | " + " | ".join(COLUMNS.values()) + " |"
    separator = "| " + " | ".join(["---"] * (len(COLUMNS) + 1)) + " |"
    lines = [
        f"# repoach and the code-review tools, compared (oldest cell checked on {oldest})",
        "",
        "Generated by `scripts/gen_comparison.py` from `docs/comparison.yaml`: edit the YAML,"
        " never this page. Every value was typed after reading the page its"
        " footnote links, on the date the footnote gives. `n/m` means not measured: the"
        " page read says nothing on that column, so this page cannot say. No score, no"
        " ranking.",
        "",
        "repoach's own row is sourced from this repository at a fixed commit, not from"
        " marketing text.",
        "",
        header,
        separator,
        *rows,
        "",
        "## PR sources, not competitors",
        "",
        "These open the pull requests that a reviewer then reads; repoach works after"
        " them, not instead of them.",
        "",
        *[f"- **{s.name}**: {s.line}" for s in comparison.pr_sources],
        "",
        "## Retired",
        "",
        *(
            [f"- **{r.name}**, last checked on {r.last_checked_on}" for r in comparison.retired]
            or ["None."]
        ),
        "",
        "## How to dispute a cell",
        "",
        f"Open an issue at [{ISSUES_URL}]({ISSUES_URL}) naming the product and the column,"
        " with the URL of the page that says otherwise and the date you read it. The"
        " cell is corrected in `docs/comparison.yaml` and the page re-rendered; a cell"
        f" read more than {comparison.constraints.source_max_age_days} days ago fails"
        " the weekly check.",
        "",
    ]
    for (url, checked_on), number in footnotes.items():
        lines.append(f"[^{number}]: [{url}]({url}), checked on {checked_on}.")
    return "\n".join(lines) + "\n"


def stale_cells(comparison: Comparison, today: date) -> list[str]:
    """Name every cell read longer ago than the freshness rule allows.

    Args:
        comparison: The validated document.
        today: The reference day.

    Returns:
        ``<product>.<column>`` for each stale cell.
    """
    limit = comparison.constraints.source_max_age_days
    return [
        f"{product.name}.{key}"
        for product in comparison.products
        for key in COLUMNS
        if (checked := product.cell(key).checked_on) is not None and (today - checked).days > limit
    ]


def _cell_refs(comparison: Comparison) -> dict[str, list[str]]:
    """Map each URL of the YAML to the ``<product>.<cell>`` names that cite it."""
    refs: dict[str, list[str]] = {}
    for product in comparison.products:
        refs.setdefault(product.vendor_url, []).append(f"{product.name}.vendor_url")
        for key in COLUMNS:
            url = product.cell(key).source_url
            if url:
                refs.setdefault(url, []).append(f"{product.name}.{key}")
    return refs


def check_page(
    comparison: Comparison,
    page: str,
    out: Path,
    yaml_name: str,
    fetch: check_links.Fetch,
) -> tuple[int, list[str]]:
    """Check every link of the rendered page, naming the YAML cell behind each dead one.

    Args:
        comparison: The validated document.
        page: The rendered page.
        out: Where the page lives; relative links resolve against it.
        yaml_name: The YAML file name printed in ``dead:`` lines.
        fetch: The HTTP transport.

    Returns:
        The link exit code and the lines to print.
    """
    report = check_links.check(check_links.extract_links(out, text=page), fetch)
    refs = _cell_refs(comparison)
    lines: list[str] = []
    named: set[str] = set()
    for result in report.dead:
        target = result.link.target
        if target in refs:
            if target not in named:
                named.add(target)
                lines.extend(f"dead: {target} ({yaml_name}: {ref})" for ref in refs[target])
        else:
            lines.append(f"{out.name}:{result.link.line} {target} {result.problem}")
    if report.exit_code == check_links.EXIT_CANNOT_SAY:
        lines.append("links: cannot say (no HTTP answer); the page is not rewritten")
    return report.exit_code, lines


def main(
    argv: Sequence[str] | None = None,
    fetch: check_links.Fetch = check_links.urllib_fetch,
    today: date | None = None,
) -> int:
    """Render and check the comparison page.

    Args:
        argv: The arguments, without the program name.
        fetch: The HTTP transport; tests pass a fake one.
        today: The reference day for staleness; defaults to the local date.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(description="Render docs/comparison.md from its YAML.")
    parser.add_argument("--yaml", type=Path, default=DEFAULT_YAML)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="render without checking")
    mode.add_argument("--check-only", action="store_true", help="check, write nothing")
    args = parser.parse_args(argv)
    try:
        comparison = load(args.yaml)
    except (OSError, ValueError) as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return EXIT_INVALID
    page = render(comparison)
    if args.offline:
        args.out.write_text(page, encoding="utf-8")
        return EXIT_OK
    link_code, lines = check_page(comparison, page, args.out, args.yaml.name, fetch)
    stale = stale_cells(comparison, today or date.today())
    for name in stale:
        lines.append(f"stale: {name}")
    if stale:
        noun = "cell" if len(stale) == 1 else "cells"
        days = comparison.constraints.source_max_age_days
        lines.append(f"stale: {len(stale)} {noun} older than {days} days")
    for line in lines:
        print(line)
    if link_code == check_links.EXIT_CANNOT_SAY:
        return EXIT_INVALID
    if link_code == check_links.EXIT_DEAD:
        return EXIT_DEAD
    if stale:
        return EXIT_STALE
    if not args.check_only:
        args.out.write_text(page, encoding="utf-8")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
