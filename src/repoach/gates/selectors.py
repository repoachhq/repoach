"""Promised-test selectors in every supported form (SP-UNIVERSAL-GATES G8).

A plan promises tests by selector.  Four forms are understood:

* pytest — ``tests/x.py::test_y`` (class segments allowed);
* Vitest — ``src/x.test.ts > name`` (``describe > it`` chains allowed;
  ``::`` accepted as the separator too);
* Go — ``./pkg -run TestY``;
* generic — ``cmd:<command>``, any command whose exit status decides.

A string of any other form parses to ``None``: callers treat it as not
applicable, never as verified.
"""

from __future__ import annotations

import re
import shlex
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from . import typescript as ts
from .commands import run_argv, run_command
from .config import GatesConfig

_JS_TEST_SUFFIXES: tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
"""Suffixes a Vitest test file may carry."""

_GO_RE = re.compile(r"^(?P<pkg>\.{1,2}(?:/[^\s]*)?)\s+-run\s+(?P<name>\S+)$")
"""``./pkg -run TestY`` (also ``./...``)."""


class SelectorKind(StrEnum):
    """The test runner a selector addresses."""

    PYTEST = "pytest"
    VITEST = "vitest"
    GO = "go"
    COMMAND = "cmd"


@dataclass(frozen=True)
class TestSelector:
    """One parsed promised-test selector.

    Attributes:
        kind: The runner.
        raw: The selector as written.
        file: Repo-relative test file (Go: the package directory;
            ``cmd``: empty).
        names: Name segments after the file (``describe``/``it`` chain,
            pytest class/function), empty for a whole-file selector.
        command: The command of a ``cmd:`` selector.
    """

    __test__ = False

    kind: SelectorKind
    raw: str
    file: str = ""
    names: tuple[str, ...] = ()
    command: str = ""

    @property
    def name(self) -> str:
        """The trailing (most specific) name, empty for a whole-file selector."""
        return self.names[-1] if self.names else ""


def is_js_test_file(path: str) -> bool:
    """Whether ``path`` names a JS/TS test file (``*.test.*``, ``*.spec.*``, ``__tests__/``)."""
    pure = PurePosixPath(path)
    if pure.suffix not in _JS_TEST_SUFFIXES:
        return False
    stem = pure.name[: -len(pure.suffix)]
    return stem.endswith((".test", ".spec")) or "__tests__" in pure.parts


def parse_selector(text: str) -> TestSelector | None:
    """Parse a promised-test selector.

    Args:
        text: The selector as written in the plan or spec.

    Returns:
        The :class:`TestSelector`, or ``None`` for an unknown form (and
        for any selector starting with ``-``, which would act as a flag).
    """
    raw = text.strip()
    if not raw or raw.startswith("-"):
        return None
    if raw.startswith("cmd:"):
        command = raw[len("cmd:") :].strip()
        return TestSelector(SelectorKind.COMMAND, raw, command=command) if command else None
    go = _GO_RE.match(raw)
    if go:
        return TestSelector(SelectorKind.GO, raw, file=go.group("pkg"), names=(go.group("name"),))
    if " > " in raw:
        file_part, *names = (part.strip() for part in raw.split(" > "))
    elif "::" in raw:
        file_part, *names = (part.strip() for part in raw.split("::"))
    else:
        file_part, names = raw, []
    if any(not name for name in names) or " " in file_part:
        return None
    if file_part.endswith(".py"):
        if " > " in raw:
            return None
        return TestSelector(SelectorKind.PYTEST, raw, file=file_part, names=tuple(names))
    if is_js_test_file(file_part):
        return TestSelector(SelectorKind.VITEST, raw, file=file_part, names=tuple(names))
    return None


def selector_file(text: str) -> str:
    """Return the file part of a selector (the raw text's ``::`` head when unparseable)."""
    parsed = parse_selector(text)
    if parsed is not None and parsed.file:
        return parsed.file
    return text.split("::", 1)[0]


def selector_present(root: Path, selector: TestSelector) -> bool:
    """Whether a non-pytest selector's file and test name exist at head.

    Args:
        root: The project root.
        selector: A parsed Vitest, Go or ``cmd`` selector (pytest
            selectors are judged by :func:`repoach.review.spec_gate.promised_present`).

    Returns:
        Vitest: the file exists and declares ``it``/``test``/``describe``
        with the trailing name.  Go: a ``*_test.go`` in the package
        declares ``func <name>(``.  ``cmd``: the command's executable
        resolves.  Pytest selectors return False here.
    """
    if selector.kind is SelectorKind.VITEST:
        target = root / selector.file
        if not target.is_file():
            return False
        if not selector.names:
            return True
        source = target.read_text(encoding="utf-8", errors="replace")
        pattern = (
            r"\b(?:it|test|describe)(?:\.\w+)*\s*\(\s*(['\"`])" + re.escape(selector.name) + r"\1"
        )
        return re.search(pattern, source) is not None
    if selector.kind is SelectorKind.GO:
        package = root / (selector.file.removesuffix("...").rstrip("/") or ".")
        pattern = re.compile(r"(?m)^func\s+" + re.escape(selector.name) + r"\s*\(")
        return any(
            pattern.search(path.read_text(encoding="utf-8", errors="replace"))
            for path in package.glob("*_test.go")
        )
    if selector.kind is SelectorKind.COMMAND:
        words = shlex.split(selector.command)
        if not words:
            return False
        head = words[0]
        return (root / head).is_file() if "/" in head else shutil.which(head) is not None
    return False


def run_selector(root: Path, selector: TestSelector, config: GatesConfig) -> tuple[bool, str]:
    """Run one non-pytest selector with its own runner.

    Args:
        root: The project root.
        selector: A parsed Vitest, Go or ``cmd`` selector.
        config: The project configuration (timeout).

    Returns:
        ``(passed, tail)``; a missing runner binary is red with
        ``command not found`` / ``binary not found``.
    """
    if selector.kind is SelectorKind.COMMAND:
        result = run_command(selector.command, root=root, timeout_s=config.timeout_s)
        return result.passed, result.tail
    if selector.kind is SelectorKind.GO:
        argv = ["go", "test", selector.file, "-run", f"^{selector.name}$"]
        result = run_argv(argv, cwd=root, timeout_s=config.timeout_s)
        return result.passed, result.tail
    if selector.kind is SelectorKind.VITEST:
        target = root / selector.file
        package = ts.package_dir(root, target.resolve())
        binary = ts.node_bin(root, package, "vitest")
        if binary is None:
            return False, f"{selector.raw}: binary not found: vitest"
        argv = [binary, "run", ts.relative_posix(target, package)]
        if selector.names:
            argv += ["-t", " ".join(selector.names)]
        result = run_argv(argv, cwd=package, timeout_s=config.timeout_s)
        return result.passed, result.tail
    return False, f"{selector.raw}: pytest selectors run through the pytest runner"


def run_selectors(root: Path, selectors: list[str], config: GatesConfig) -> tuple[bool, str]:
    """Run every non-pytest selector; red on the first failure or unknown form.

    Args:
        root: The project root.
        selectors: Raw selectors (pytest ones are refused — the caller
            runs them through pytest).
        config: The project configuration.

    Returns:
        ``(passed, tail)``.
    """
    tails: list[str] = []
    for raw in selectors:
        selector = parse_selector(raw)
        if selector is None:
            return False, f"not applicable: unknown selector form {raw!r}"
        passed, tail = run_selector(root, selector, config)
        tails.append(tail)
        if not passed:
            return False, tail
    return True, "\n".join(tails)


__all__ = [
    "SelectorKind",
    "TestSelector",
    "is_js_test_file",
    "parse_selector",
    "run_selector",
    "run_selectors",
    "selector_file",
    "selector_present",
]
