"""Pins the ``## Measured`` section of the PR template (AC14 of
SP-AUTHOR-CLAIMS-ARE-MEASURED).

The claim extractor that will parse ``format:pr-measured-section`` for real
(``src/repoach/review/author_claims.py``) does not exist yet — that spec is
still a draft. This test locks what is checkable without it: the section's
presence in ``docs/PR_TEMPLATE.md``, the byte-for-byte equality required of
``.github/pull_request_template.md``, and a mechanical shape check of the
example lines against the documented grammar (``- `<command>` → <expected>``).
Once the real extractor lands, a follow-up test should assert every example
line parses through it instead of this regex approximation.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS_TEMPLATE = _REPO_ROOT / "docs" / "PR_TEMPLATE.md"
_GITHUB_TEMPLATE = _REPO_ROOT / ".github" / "pull_request_template.md"

_MEASURED_LINE_RE = re.compile(r"^- `[^`]+` (?:→|->) .+$")


def test_docs_template_has_measured_section() -> None:
    text = _DOCS_TEMPLATE.read_text(encoding="utf-8")
    assert "## Measured" in text


def test_github_template_is_byte_equal_to_docs_template() -> None:
    assert _GITHUB_TEMPLATE.read_bytes() == _DOCS_TEMPLATE.read_bytes()


def test_measured_section_example_lines_match_the_grammar_shape() -> None:
    text = _DOCS_TEMPLATE.read_text(encoding="utf-8")
    section = text.split("## Measured", 1)[1].split("\n## ", 1)[0]
    example_lines = [line for line in section.splitlines() if line.startswith("- `")]
    assert example_lines
    for line in example_lines:
        assert _MEASURED_LINE_RE.match(line), line
