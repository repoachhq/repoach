"""Render ``repoach.toml`` from the shipped template (SP-UNIVERSAL-PROJECT-CONFIG A1).

No TOML writer dependency: the template
(``repoach.toml.template``, next to this module) fixes the key order and
the explanatory comments, every key written as a commented-out default.
:func:`render_project_toml` uncomments and fills exactly the keys it is
given and leaves every other line byte-identical, so the output of two
runs over the same measurements is byte-identical too.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

TEMPLATE_PATH = Path(__file__).with_name("repoach.toml.template")
"""The shipped template, packaged with the module."""

TomlScalar = str | bool | int | list[str] | dict[str, str]

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_SECTION_RE = re.compile(r"^\[(?P<name>[a-z_]+)\]\s*$")
_KEY_RE = re.compile(r"^(?:#\s*)?(?P<key>[a-z_]+)\s*=")


def toml_value(value: TomlScalar) -> str:
    """Return ``value`` as a TOML literal.

    Args:
        value: A string, boolean, integer, list of strings, or a
            string-to-string table (written inline).

    Returns:
        The literal: basic strings use JSON escaping, which TOML basic
        strings accept.

    Raises:
        TypeError: Any other type.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "[" + ", ".join(json.dumps(item, ensure_ascii=False) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(item, str) for item in value.values()):
        pairs = [
            f"{key if _BARE_KEY_RE.match(key) else json.dumps(key)} = "
            f"{json.dumps(item, ensure_ascii=False)}"
            for key, item in value.items()
        ]
        return "{" + ", ".join(pairs) + "}" if pairs else "{}"
    raise TypeError(f"cannot write {type(value).__name__} to repoach.toml")


def template_text() -> str:
    """Return the shipped template.

    Returns:
        The template content.
    """
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def render_project_toml(values: Mapping[str, TomlScalar], template: str | None = None) -> str:
    """Fill ``values`` into the template.

    Args:
        values: ``{"section.key": value}`` for every key to write
            uncommented; every other key stays a commented default.
        template: Template text; the shipped one when None.

    Returns:
        The file content.

    Raises:
        KeyError: A key of ``values`` has no line in the template.
    """
    text = template if template is not None else template_text()
    pending = dict(values)
    section = ""
    out: list[str] = []
    for line in text.splitlines():
        header = _SECTION_RE.match(line)
        if header is not None:
            section = header.group("name")
            out.append(line)
            continue
        match = _KEY_RE.match(line)
        if match is not None:
            key = match.group("key")
            dotted = f"{section}.{key}"
            if dotted in pending:
                out.append(f"{key} = {toml_value(pending.pop(dotted))}")
                continue
        out.append(line)
    if pending:
        raise KeyError(f"no template line for {sorted(pending)}")
    return "\n".join(out) + "\n"
