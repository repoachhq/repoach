"""TypeScript / JavaScript project helpers for the verifiers (SP-UNIVERSAL-GATES).

Everything the TypeScript verifier family needs that is not a
subprocess: reading ``tsconfig.json`` (JSONC — comments and trailing
commas allowed, ``extends`` and solution-style ``references``
followed), finding the package directory and the project's own
``node_modules/.bin`` tools, resolving ``import … from`` specifiers
through relative paths and ``compilerOptions.paths``/``baseUrl``, and
spotting exported symbols that carry no JSDoc block.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ..core.logging import get_logger

_log = get_logger(__name__)

_RESOLVE_EXTENSIONS: tuple[str, ...] = (
    ".ts",
    ".tsx",
    ".d.ts",
    ".mts",
    ".cts",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".json",
)
"""Suffixes tried, in order, when an import specifier names no extension."""

_JS_TO_TS: dict[str, tuple[str, ...]] = {
    ".js": (".ts", ".tsx"),
    ".jsx": (".tsx",),
    ".mjs": (".mts",),
    ".cjs": (".cts",),
}
"""ESM-style ``./x.js`` imports that TypeScript maps onto ``x.ts`` sources."""

_IMPORT_RE = re.compile(
    r"""(?:^|[;\s])(?:import|export)\s+(?:type\s+)?(?:[\w*{}\s,$]+\s+from\s+)?["']([^"'\n]+)["']"""
    r"""|\bimport\s*\(\s*["']([^"'\n]+)["']\s*\)"""
    r"""|\brequire\s*\(\s*["']([^"'\n]+)["']\s*\)""",
    re.MULTILINE,
)
"""Static ``import``/``export … from``, dynamic ``import()`` and ``require()``."""

_EXPORT_RE = re.compile(
    r"^export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:function\*?|class|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
"""A top-level exported declaration and its name."""

_TSC_LINE_RE = re.compile(r"^(?P<file>[^\s(][^(]*)\((?P<line>\d+),(?P<col>\d+)\): (?P<rest>.+)$")
"""``src/x.ts(3,7): error TS2322: …`` — tsc's default diagnostic line."""


def strip_jsonc(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments and trailing commas from JSONC.

    String literals are respected, so ``"@/*"`` and ``"./src/*"`` stay
    intact.

    Args:
        text: JSONC source (``tsconfig.json`` dialect).

    Returns:
        Plain JSON text.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            end = index + 1
            while end < length and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            out.append(text[index : end + 1])
            index = end + 1
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline < 0 else newline
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = length if close < 0 else close + 2
        else:
            out.append(char)
            index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def read_jsonc(path: Path) -> dict[str, Any]:
    """Parse a JSONC file into a dict.

    Raises:
        ValueError: The file is not a JSON object after comment stripping.
        OSError: The file cannot be read.
    """
    data = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


@dataclass(frozen=True)
class TsConfig:
    """The resolution-relevant part of one ``tsconfig``.

    Attributes:
        path: The config file.
        base_dir: Directory ``paths`` targets resolve against (``baseUrl``
            when set, else the declaring config's directory).
        paths: ``compilerOptions.paths`` patterns and their targets.
        includes: ``include`` globs, relative to the config's directory.
    """

    path: Path
    base_dir: Path
    paths: dict[str, tuple[str, ...]] = field(default_factory=dict)
    includes: tuple[str, ...] = ()


def _load_options(path: Path, depth: int = 0) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    """Return ``(compilerOptions, paths_base, raw)`` following ``extends`` chains."""
    raw = read_jsonc(path)
    options: dict[str, Any] = {}
    paths_base = path.parent
    parent = raw.get("extends")
    if isinstance(parent, str) and parent.startswith(".") and depth < 8:
        parent_path = (path.parent / parent).resolve()
        if parent_path.suffix != ".json":
            parent_path = parent_path.with_name(parent_path.name + ".json")
        if parent_path.is_file():
            options, paths_base, _ = _load_options(parent_path, depth + 1)
    own = raw.get("compilerOptions")
    if isinstance(own, dict):
        options = {**options, **own}
        if "baseUrl" in own or "paths" in own:
            paths_base = path.parent
    return options, paths_base, raw


def load_tsconfig(path: Path) -> TsConfig:
    """Load the resolution settings of the tsconfig at ``path``.

    Args:
        path: A ``tsconfig*.json`` file.

    Returns:
        The :class:`TsConfig`.
    """
    options, paths_base, raw = _load_options(path)
    base_url = options.get("baseUrl")
    base_dir = (paths_base / base_url).resolve() if isinstance(base_url, str) else paths_base
    paths: dict[str, tuple[str, ...]] = {}
    declared = options.get("paths")
    if isinstance(declared, dict):
        for pattern, targets in declared.items():
            if isinstance(targets, list):
                paths[str(pattern)] = tuple(str(t) for t in targets if isinstance(t, str))
    includes_raw = raw.get("include")
    includes = (
        tuple(str(i) for i in includes_raw if isinstance(i, str))
        if isinstance(includes_raw, list)
        else ()
    )
    return TsConfig(path=path, base_dir=base_dir, paths=paths, includes=includes)


def _include_covers(config_dir: Path, includes: tuple[str, ...], file: Path) -> bool:
    """Whether a referenced project's ``include`` list covers ``file``."""
    if not includes:
        return True
    for pattern in includes:
        static = pattern.split("*", 1)[0].rstrip("/")
        base = (config_dir / static).resolve() if static else config_dir.resolve()
        if file == base or base in file.parents:
            return True
    return False


def find_tsconfig(root: Path, file: Path) -> Path | None:
    """Return the tsconfig governing ``file``, walking up to ``root``.

    A solution-style config (``"files": []`` plus ``references``) is
    replaced by the referenced project whose ``include`` covers the file.

    Args:
        root: The project root (the walk stops there).
        file: Absolute path of the source file.

    Returns:
        The config path, or ``None`` when no ``tsconfig.json`` exists
        between the file and the root.
    """
    root = root.resolve()
    for directory in [file.parent, *file.parent.parents]:
        candidate = directory / "tsconfig.json"
        if candidate.is_file():
            return _pick_reference(candidate, file)
        if directory == root or root not in directory.parents:
            break
    return None


def _pick_reference(config_path: Path, file: Path) -> Path:
    """Follow a solution config's ``references`` to the project covering ``file``."""
    try:
        raw = read_jsonc(config_path)
    except (OSError, ValueError) as exc:
        _log.warning("gates.tsconfig_unreadable", path=str(config_path), error=str(exc)[:200])
        return config_path
    references = raw.get("references")
    if not isinstance(references, list) or not references:
        return config_path
    for reference in references:
        ref_path_raw = reference.get("path") if isinstance(reference, dict) else None
        if not isinstance(ref_path_raw, str):
            continue
        ref_path = (config_path.parent / ref_path_raw).resolve()
        if ref_path.is_dir():
            ref_path = ref_path / "tsconfig.json"
        if not ref_path.is_file():
            continue
        try:
            ref_raw = read_jsonc(ref_path)
        except (OSError, ValueError) as exc:
            _log.warning("gates.tsconfig_unreadable", path=str(ref_path), error=str(exc)[:200])
            continue
        includes_raw = ref_raw.get("include")
        includes = (
            tuple(str(i) for i in includes_raw if isinstance(i, str))
            if isinstance(includes_raw, list)
            else ()
        )
        if _include_covers(ref_path.parent, includes, file.resolve()):
            return ref_path
    return config_path


def package_dir(root: Path, file: Path) -> Path:
    """Return the nearest ancestor of ``file`` holding a ``package.json`` (else ``root``)."""
    root = root.resolve()
    for directory in [file.parent, *file.parent.parents]:
        if (directory / "package.json").is_file():
            return directory
        if directory == root or root not in directory.parents:
            break
    return root


def node_bin(root: Path, start: Path, name: str) -> str | None:
    """Locate a Node tool: the project's ``node_modules/.bin`` first, then ``PATH``.

    Args:
        root: The project root (the upward search stops there).
        start: Directory the search starts from (the package directory).
        name: Tool name (``tsc``, ``eslint``, ``vitest``).

    Returns:
        The executable path, or ``None`` when absent everywhere.
    """
    root = root.resolve()
    for directory in [start.resolve(), *start.resolve().parents]:
        candidate = directory / "node_modules" / ".bin" / name
        if candidate.is_file():
            return str(candidate)
        if directory == root or root not in directory.parents:
            break
    return shutil.which(name)


def import_specifiers(source: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, specifier)`` for every import in ``source``."""
    for match in _IMPORT_RE.finditer(source):
        specifier = match.group(1) or match.group(2) or match.group(3)
        if specifier:
            yield source.count("\n", 0, match.start(0) + 1) + 1, specifier


def _candidates(base: Path) -> Iterator[Path]:
    """Yield the files an extension-less or ESM-style specifier may denote."""
    yield base
    mapped = _JS_TO_TS.get(base.suffix)
    if mapped:
        for ext in mapped:
            yield base.with_suffix(ext)
    for ext in _RESOLVE_EXTENSIONS:
        yield base.with_name(base.name + ext)
    for ext in _RESOLVE_EXTENSIONS:
        yield base / f"index{ext}"


def _first_file(base: Path) -> Path | None:
    """Return the first existing file among :func:`_candidates` of ``base``."""
    for candidate in _candidates(base):
        if candidate.is_file():
            return candidate
    return None


def _alias_targets(config: TsConfig, specifier: str) -> list[Path] | None:
    """Return the bases a ``paths`` alias maps ``specifier`` to (``None``: no alias)."""
    for pattern, targets in config.paths.items():
        if "*" in pattern:
            prefix, _, suffix = pattern.partition("*")
            if specifier.startswith(prefix) and specifier.endswith(suffix):
                middle = specifier[len(prefix) : len(specifier) - len(suffix)]
                return [config.base_dir / t.replace("*", middle) for t in targets]
        elif specifier == pattern:
            return [config.base_dir / t for t in targets]
    return None


def resolve_import(file: Path, specifier: str, config: TsConfig | None) -> tuple[bool, Path | None]:
    """Resolve one import specifier of ``file``.

    Args:
        file: Absolute path of the importing file.
        specifier: The module string.
        config: The governing tsconfig, if any.

    Returns:
        ``(checked, target)``: ``checked`` is False for a bare package
        specifier (left to the package manager); ``target`` is the
        resolved file, ``None`` when a relative or aliased import
        resolves to nothing.
    """
    if specifier.startswith((".", "/")):
        base = (file.parent / specifier) if specifier.startswith(".") else Path(specifier)
        return True, _first_file(base.resolve())
    if config is not None:
        bases = _alias_targets(config, specifier)
        if bases is not None:
            for base in bases:
                found = _first_file(base.resolve())
                if found is not None:
                    return True, found
            return True, None
    return False, None


def undocumented_exports(source: str) -> list[tuple[int, str]]:
    """Return ``(line, name)`` for exported declarations with no JSDoc block above."""
    lines = source.splitlines()
    missing: list[tuple[int, str]] = []
    for match in _EXPORT_RE.finditer(source):
        line_no = source.count("\n", 0, match.start()) + 1
        previous = line_no - 2
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        while previous >= 0 and lines[previous].lstrip().startswith("@"):
            previous -= 1
        documented = previous >= 0 and lines[previous].rstrip().endswith("*/")
        if documented:
            opening = previous
            while opening >= 0 and "/*" not in lines[opening]:
                opening -= 1
            documented = opening >= 0 and "/**" in lines[opening]
        if not documented:
            missing.append((line_no, match.group(1)))
    return missing


def tsc_diagnostics(output: str, config_dir: Path, root: Path) -> list[str]:
    """Rewrite tsc's ``file(line,col): msg`` lines to repo-relative ``file:line:col: msg``."""
    out: list[str] = []
    for raw in output.splitlines():
        match = _TSC_LINE_RE.match(raw.strip())
        if match is None:
            continue
        target = (config_dir / match.group("file")).resolve()
        try:
            shown = target.relative_to(root.resolve()).as_posix()
        except ValueError:
            shown = match.group("file")
        out.append(f"{shown}:{match.group('line')}:{match.group('col')}: {match.group('rest')}")
    return out


def relative_posix(path: Path, start: Path) -> str:
    """Return ``path`` relative to ``start`` as a POSIX string."""
    return PurePosixPath(path.resolve().relative_to(start.resolve())).as_posix()


__all__ = [
    "TsConfig",
    "find_tsconfig",
    "import_specifiers",
    "load_tsconfig",
    "node_bin",
    "package_dir",
    "read_jsonc",
    "relative_posix",
    "resolve_import",
    "strip_jsonc",
    "tsc_diagnostics",
    "undocumented_exports",
]
