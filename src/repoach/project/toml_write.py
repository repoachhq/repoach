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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypeAlias, TypeGuard

TEMPLATE_PATH = Path(__file__).with_name("repoach.toml.template")
"""The shipped template, packaged with the module."""

TomlValue: TypeAlias = "str | bool | int | Sequence[TomlValue] | Mapping[str, TomlValue]"
"""A value ``repoach.toml`` holds: a string, boolean or integer, or a list or table of them.

Recursive, so an array of tables (``[[review.generated]]``) is one; the
containers are the read-only ``Sequence`` / ``Mapping`` so a
``list[str]`` or a ``dict[str, str]`` is one too.
"""

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_SECTION_RE = re.compile(r"^\[(?P<name>[a-z_]+)\]\s*$")
_KEY_RE = re.compile(r"^(?:#\s*)?(?P<key>[a-z_]+)\s*=")


def toml_value(value: TomlValue) -> str:
    """Return ``value`` as a TOML literal.

    Lists and tables nest: an array of tables (``[[review.generated]]``)
    is written as an inline array of inline tables, which TOML parses to
    the same value.

    Args:
        value: A string, boolean, integer, list, or string-keyed table,
            nested to any depth.

    Returns:
        The literal: basic strings use JSON escaping, which TOML basic
        strings accept.

    Raises:
        TypeError: Any other type, at any depth.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        pairs = [f"{toml_key(key)} = {toml_value(item)}" for key, item in value.items()]
        return "{" + ", ".join(pairs) + "}" if pairs else "{}"
    raise TypeError(f"cannot write {type(value).__name__} to repoach.toml")


def toml_key(key: str) -> str:
    """Return ``key`` bare when TOML allows it, quoted otherwise.

    Args:
        key: A table key.

    Returns:
        The key as it may stand left of ``=``.
    """
    return key if _BARE_KEY_RE.match(key) else json.dumps(key, ensure_ascii=False)


def is_array_of_tables(value: object) -> TypeGuard[list[dict[str, TomlValue]]]:
    """Tell whether ``value`` is written as ``[[section.key]]`` blocks.

    Args:
        value: An effective configuration value.

    Returns:
        True for a non-empty list whose every item is a string-keyed
        table; an empty list, or one mixing tables and other values, is
        written inline.
    """
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, dict) and all(isinstance(k, str) for k in item) for item in value)
    )


def array_of_tables_lines(
    section: str, key: str, entries: Sequence[Mapping[str, TomlValue]]
) -> list[str]:
    """Write ``entries`` as ``[[section.key]]`` blocks, each preceded by a blank line.

    Args:
        section: The table the array belongs to (``review``).
        key: The array's key (``generated``).
        entries: One table per block.

    Returns:
        The lines, plain: a caller annotating them does it on its side.
    """
    lines: list[str] = []
    for entry in entries:
        lines.extend(["", f"[[{section}.{toml_key(key)}]]"])
        lines.extend(f"{toml_key(name)} = {toml_value(item)}" for name, item in entry.items())
    return lines


def template_text() -> str:
    """Return the shipped template.

    Returns:
        The template content.
    """
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def render_project_toml(values: Mapping[str, TomlValue], template: str | None = None) -> str:
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
