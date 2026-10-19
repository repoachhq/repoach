"""Language detection and the per-language verifier table (SP-UNIVERSAL-GATES).

One :class:`LanguageSpec` row per language names its extensions, the
shebang interpreters that identify an extension-less script, the
verifiers that check it and the test-selector form its tests use.  The
table is data: supporting a new language is one more row (plus, when
its verifier is a plain command, one :class:`CommandVerifier` row in
:mod:`repoach.gates.verifiers`).

Detection is by path: the extension first, then — for an extension-less
file such as a git hook — the interpreter named on its shebang line.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath


class Language(StrEnum):
    """A language a project can declare in ``[project].languages``."""

    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    GO = "go"
    RUST = "rust"
    VERILOG = "verilog"
    C = "c"
    SHELL = "shell"
    YAML = "yaml"
    MARKDOWN = "markdown"
    OTHER = "other"


@dataclass(frozen=True)
class LanguageSpec:
    """How one language is recognised and verified.

    Attributes:
        language: The language.
        extensions: Lower-case suffixes (with the dot) that identify it.
        shebangs: Interpreter basenames that identify an extension-less script.
        verifiers: Names of the verifiers that check its files, in run order.
        selector_kind: Test-selector form its tests use (``""`` when none).
    """

    language: Language
    extensions: tuple[str, ...]
    shebangs: tuple[str, ...] = ()
    verifiers: tuple[str, ...] = ()
    selector_kind: str = ""


LANGUAGE_TABLE: tuple[LanguageSpec, ...] = (
    LanguageSpec(
        Language.PYTHON,
        (".py",),
        ("python", "python3"),
        ("python_syntax", "python_imports", "python_house_lint", "python_docstring"),
        "pytest",
    ),
    LanguageSpec(
        Language.TYPESCRIPT,
        (".ts", ".tsx", ".mts", ".cts"),
        (),
        ("tsc", "eslint", "ts_imports", "jsdoc"),
        "vitest",
    ),
    LanguageSpec(
        Language.JAVASCRIPT,
        (".js", ".jsx", ".mjs", ".cjs"),
        ("node",),
        ("eslint", "ts_imports", "jsdoc"),
        "vitest",
    ),
    LanguageSpec(Language.GO, (".go",), (), (), "go"),
    LanguageSpec(Language.RUST, (".rs",), (), (), ""),
    LanguageSpec(Language.VERILOG, (".v", ".sv", ".vh", ".svh"), (), ("verilator",), ""),
    LanguageSpec(Language.C, (".c", ".h"), (), ("gcc",), ""),
    LanguageSpec(
        Language.SHELL, (".sh", ".bash"), ("sh", "bash", "dash"), ("bash_n", "shellcheck")
    ),
    LanguageSpec(Language.YAML, (".yml", ".yaml"), (), ("yaml_parse",)),
    LanguageSpec(Language.MARKDOWN, (".md", ".markdown"), (), ("md_links",)),
)
"""Every supported language, in display order."""

_BY_LANGUAGE: dict[Language, LanguageSpec] = {spec.language: spec for spec in LANGUAGE_TABLE}
_BY_EXTENSION: dict[str, Language] = {
    ext: spec.language for spec in LANGUAGE_TABLE for ext in spec.extensions
}
_BY_SHEBANG: dict[str, Language] = {
    name: spec.language for spec in LANGUAGE_TABLE for name in spec.shebangs
}
_SHEBANG_READ_BYTES = 256
"""Bytes read from an extension-less file to find its shebang line."""


def spec_for(language: Language) -> LanguageSpec | None:
    """Return the table row of ``language`` (``None`` for :attr:`Language.OTHER`)."""
    return _BY_LANGUAGE.get(language)


def _shebang_language(path: Path) -> Language:
    """Return the language named by ``path``'s shebang interpreter, else OTHER."""
    try:
        with path.open("rb") as handle:
            head = handle.read(_SHEBANG_READ_BYTES)
    except OSError:
        return Language.OTHER
    if not head.startswith(b"#!"):
        return Language.OTHER
    line = head.splitlines()[0][2:].decode("utf-8", "replace").strip()
    words = line.split()
    if not words:
        return Language.OTHER
    interpreter = PurePosixPath(words[0]).name
    if interpreter == "env":
        interpreter = next((word for word in words[1:] if not word.startswith("-")), "")
    if interpreter not in _BY_SHEBANG:
        interpreter = interpreter.rstrip("0123456789.")
    return _BY_SHEBANG.get(interpreter, Language.OTHER)


def language_of(path: str, root: Path | None = None) -> Language:
    """Return the language of a repo-relative ``path``.

    Args:
        path: Repo-relative path.
        root: Project root; when given, an extension-less file's shebang
            is read to recognise scripts and git hooks.

    Returns:
        The detected language, :attr:`Language.OTHER` when unknown.
    """
    pure = PurePosixPath(path)
    suffix = pure.suffix.lower()
    if suffix:
        return _BY_EXTENSION.get(suffix, Language.OTHER)
    if root is None:
        return Language.OTHER
    target = root / path
    if not target.is_file():
        return Language.OTHER
    return _shebang_language(target)


def diff_languages(paths: list[str], root: Path | None = None) -> frozenset[Language]:
    """Return the languages present among a diff's ``paths``."""
    return frozenset(language_of(path, root) for path in paths)


def _under(path: str, roots: tuple[str, ...]) -> bool:
    """Whether ``path`` sits under one of ``roots`` (empty roots = anywhere)."""
    if not roots:
        return True
    for root in roots:
        prefix = root.strip("/")
        if prefix in ("", "."):
            return True
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def python_applicable(
    path: str, languages: frozenset[Language], roots: tuple[str, ...]
) -> tuple[bool, str]:
    """Decide whether a Python-only verifier may run on ``path``.

    Args:
        path: Repo-relative path the verifier would read.
        languages: Languages the project declares.
        roots: Roots a ``.py`` file must sit under (empty = anywhere).

    Returns:
        ``(True, "")`` when the verifier applies, else ``(False,
        reason)`` with ``not python: <suffix>``, ``python not declared``
        or ``outside src_roots``.
    """
    suffix = PurePosixPath(path).suffix.lower()
    if suffix != ".py":
        return False, f"not python: {suffix or '(no extension)'}"
    if Language.PYTHON not in languages:
        return False, "python not declared"
    if not _under(path.replace("\\", "/").removeprefix("./"), roots):
        return False, "outside src_roots"
    return True, ""


__all__ = [
    "LANGUAGE_TABLE",
    "Language",
    "LanguageSpec",
    "diff_languages",
    "language_of",
    "python_applicable",
    "spec_for",
]
