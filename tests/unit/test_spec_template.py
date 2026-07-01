"""Contract test for the canonical spec template (SP-SPEC-TEMPLATE).

Pins the `format:spec-frontmatter` contract the architecture tooling
(SP-ARCH-GRAPH deriver, SP-ARCH-EDGE-GATE) resolves edges through: the
template must declare the required frontmatter keys and the
`owns: {code, resources}` shape (AC2), and its guidance must document the
resource-key grammar and the `id == SP-ID` convention (AC3).
"""

from __future__ import annotations

from pathlib import Path

import yaml

_TEMPLATE = Path(__file__).resolve().parents[2] / "docs" / "specs" / "_TEMPLATE.md"

_REQUIRED_KEYS = ("id", "title", "version", "status", "owns", "depends_on")
_OWNS_SUBKEYS = ("code", "resources")
_RESOURCE_KINDS = ("db:table:", "queue:topic:", "format:")


def _frontmatter() -> dict[str, object]:
    """Parse the YAML frontmatter delimited by the first two ``---`` fence lines.

    The closing fence is a line that is exactly ``---``; a ``# --- … ---``
    guidance comment inside the block is not a fence.
    """
    lines = _TEMPLATE.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0].strip() == "---", "template must open with a --- fence"
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    assert end is not None, "template frontmatter must be closed by a --- fence line"
    loaded = yaml.safe_load("\n".join(lines[1:end]))
    assert isinstance(loaded, dict), "frontmatter must parse to a mapping"
    return loaded


def test_template_declares_required_frontmatter_keys() -> None:
    front = _frontmatter()
    missing = [key for key in _REQUIRED_KEYS if key not in front]
    assert not missing, f"template frontmatter is missing required keys: {missing}"


def test_owns_has_code_and_resources_shape() -> None:
    owns = _frontmatter()["owns"]
    assert isinstance(owns, dict), "owns must be a mapping of {code, resources}"
    missing = [key for key in _OWNS_SUBKEYS if key not in owns]
    assert not missing, f"owns is missing sub-keys: {missing}"


def test_guidance_documents_resource_grammar_and_id_convention() -> None:
    text = _TEMPLATE.read_text(encoding="utf-8")
    for kind in _RESOURCE_KINDS:
        assert kind in text, f"template must document the resource kind {kind!r}"
    assert "id` IS the SP-ID" in text or "id == SP-ID" in text, (
        "template guidance must state the id == SP-ID convention"
    )
