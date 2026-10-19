"""Per-language file verifiers behind one interface (SP-UNIVERSAL-GATES).

:func:`run_verifiers` takes the paths a change touched, groups them by
:func:`~repoach.gates.languages.language_of`, and runs each declared
language's verifiers from :data:`~repoach.gates.languages.LANGUAGE_TABLE`.
Every verifier answers with :class:`VerifierOutcome` rows:

* ``pass`` / ``fail`` — the check ran; a ``fail`` turns the gate red
  and its detail carries ``file:line`` diagnostics;
* ``advisory`` — a finding that is reported but never blocks (JSDoc on
  exports, or any verifier listed in ``[gates].advisory_verifiers``);
* ``not_applicable`` — the check could not run, with the reason named:
  the language is not declared, the tool binary is missing
  (``binary not found: verilator``), the project has no config for it.

Two kinds of verifier exist.  A :class:`CommandVerifier` is pure data —
a binary and an argv template with ``{file}`` — so wiring one more
external checker is one table row.  A :class:`FunctionVerifier` holds
in-process logic (the TypeScript family, YAML, Markdown links).  The
Python verifiers are listed for completeness as :class:`InProcessVerifier`:
they run inside the Coder / Developer Python chain and the finding
verifiers, not here.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from shutil import which
from urllib.parse import unquote

import yaml

from ..core.logging import get_logger
from . import typescript as ts
from .commands import run_argv
from .config import GatesConfig
from .languages import LANGUAGE_TABLE, Language, language_of, spec_for

_log = get_logger(__name__)

VERIFIER_TIMEOUT_S = 600
"""Seconds one external verifier invocation may run."""

_DETAIL_LINES = 40
"""Diagnostic lines kept per outcome."""


class VerifierStatus(StrEnum):
    """Outcome of one verifier over one group of files."""

    PASS = "pass"
    FAIL = "fail"
    ADVISORY = "advisory"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class VerifierOutcome:
    """What one verifier concluded about some files.

    Attributes:
        verifier: Verifier name (a key of :data:`VERIFIERS`).
        language: Language of the files.
        status: The conclusion.
        detail: Reason or diagnostics (``file:line: message`` lines).
        paths: Repo-relative paths the outcome covers.
    """

    verifier: str
    language: Language
    status: VerifierStatus
    detail: str
    paths: tuple[str, ...] = ()

    def render(self) -> str:
        """Return a one-header, indented-detail text block."""
        head = f"[{self.status.value}] {self.language.value}/{self.verifier}"
        if self.paths:
            head += f" ({len(self.paths)} file(s))"
        if not self.detail:
            return head
        body = "\n".join(f"  {line}" for line in self.detail.splitlines()[:_DETAIL_LINES])
        return f"{head}\n{body}"


@dataclass(frozen=True)
class VerifierReport:
    """Every outcome of one :func:`run_verifiers` call."""

    outcomes: tuple[VerifierOutcome, ...] = ()

    @property
    def passed(self) -> bool:
        """True when no outcome is a blocking ``fail``."""
        return not any(o.status is VerifierStatus.FAIL for o in self.outcomes)

    def count(self, status: VerifierStatus) -> int:
        """Return how many outcomes carry ``status``."""
        return sum(1 for o in self.outcomes if o.status is status)

    def render(self) -> str:
        """Return the full text report, failures first."""
        order = {
            VerifierStatus.FAIL: 0,
            VerifierStatus.ADVISORY: 1,
            VerifierStatus.NOT_APPLICABLE: 2,
            VerifierStatus.PASS: 3,
        }
        ordered = sorted(self.outcomes, key=lambda o: order[o.status])
        summary = ", ".join(
            f"{self.count(status)} {status.value}"
            for status in VerifierStatus
            if self.count(status)
        )
        return "\n".join(
            [f"verifiers: {summary or 'nothing to check'}"] + [o.render() for o in ordered]
        )


VerifierFn = Callable[[Path, list[str], GatesConfig], list[VerifierOutcome]]


@dataclass(frozen=True)
class CommandVerifier:
    """An external checker run once per file; pure data.

    Attributes:
        name: Verifier name.
        language: Language it checks.
        binary: Executable looked up on ``PATH``.
        argv: Arguments after the binary; ``{file}`` is the repo-relative path.
        advisory_unless_declared: Failures are advisory unless the project
            declares the verifier's command in ``[gates].verifier_commands``.
            Set for checkers whose default argv cannot see the project's
            include paths or generated sources (a Verilog design that
            instantiates an hls4ml-generated module, a C file whose headers
            live under ``-I`` dirs): blocking on those would red every gate.
    """

    name: str
    language: Language
    binary: str
    argv: tuple[str, ...]
    advisory_unless_declared: bool = False


@dataclass(frozen=True)
class FunctionVerifier:
    """An in-process verifier with its own logic.

    Attributes:
        name: Verifier name.
        language: Language it checks.
        fn: ``(root, paths, config) -> outcomes``.
        binaries: External tools it needs (shown by ``doctor --gates``).
        advisory: Whether its failures never block.
    """

    name: str
    language: Language
    fn: VerifierFn
    binaries: tuple[str, ...] = ()
    advisory: bool = False


@dataclass(frozen=True)
class InProcessVerifier:
    """A verifier that runs inside the review chain, listed for the table.

    Attributes:
        name: Verifier name.
        language: Language it checks.
        where: Where it runs.
    """

    name: str
    language: Language
    where: str


Verifier = CommandVerifier | FunctionVerifier | InProcessVerifier


def _outcome(
    name: str,
    language: Language,
    status: VerifierStatus,
    detail: str,
    paths: Iterable[str] = (),
) -> VerifierOutcome:
    """Build a :class:`VerifierOutcome` with a sorted, de-duplicated path tuple."""
    return VerifierOutcome(name, language, status, detail, tuple(sorted(set(paths))))


def _group(
    root: Path, paths: list[str], locate: Callable[[Path], Path | None]
) -> dict[Path | None, list[str]]:
    """Group ``paths`` by the directory/config ``locate`` returns for each."""
    groups: dict[Path | None, list[str]] = {}
    for path in paths:
        groups.setdefault(locate((root / path).resolve()), []).append(path)
    return groups


def _verify_tsc(root: Path, paths: list[str], config: GatesConfig) -> list[VerifierOutcome]:
    """Type-check each tsconfig project the paths belong to with ``tsc --noEmit``."""
    outcomes: list[VerifierOutcome] = []
    for tsconfig, files in _group(root, paths, lambda f: ts.find_tsconfig(root, f)).items():
        if tsconfig is None:
            outcomes.append(
                _outcome(
                    "tsc",
                    Language.TYPESCRIPT,
                    VerifierStatus.NOT_APPLICABLE,
                    "no tsconfig.json above the file(s)",
                    files,
                )
            )
            continue
        binary = ts.node_bin(root, ts.package_dir(root, tsconfig), "tsc")
        if binary is None:
            outcomes.append(
                _outcome(
                    "tsc",
                    Language.TYPESCRIPT,
                    VerifierStatus.NOT_APPLICABLE,
                    "binary not found: tsc",
                    files,
                )
            )
            continue
        result = run_argv(
            [binary, "--noEmit", "-p", tsconfig.name],
            cwd=tsconfig.parent,
            timeout_s=min(config.timeout_s, VERIFIER_TIMEOUT_S),
        )
        if result.passed:
            outcomes.append(_outcome("tsc", Language.TYPESCRIPT, VerifierStatus.PASS, "", files))
            continue
        diagnostics = ts.tsc_diagnostics(result.tail, tsconfig.parent, root)
        changed = set(files)
        diagnostics.sort(key=lambda line: line.split(":", 1)[0] not in changed)
        detail = "\n".join(diagnostics) if diagnostics else result.tail
        outcomes.append(_outcome("tsc", Language.TYPESCRIPT, VerifierStatus.FAIL, detail, files))
    return outcomes


_ESLINT_CONFIGS: tuple[str, ...] = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    "eslint.config.mts",
    "eslint.config.cts",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    ".eslintrc",
)
"""Files that mark a directory as an ESLint project root."""


def _eslint_dir(root: Path, file: Path) -> Path | None:
    """Return the nearest ancestor of ``file`` holding an ESLint config."""
    top = root.resolve()
    for directory in [file.parent, *file.parent.parents]:
        if any((directory / name).is_file() for name in _ESLINT_CONFIGS):
            return directory
        if directory == top or top not in directory.parents:
            break
    return None


def _eslint_messages(stdout: str, root: Path) -> tuple[list[str], list[str]] | None:
    """Split ESLint's JSON report into ``(errors, warnings)`` lines; ``None`` if not JSON."""
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as exc:
        _log.info("gates.eslint_report_not_json", error=str(exc)[:200])
        return None
    if not isinstance(report, list):
        return None
    errors: list[str] = []
    warnings: list[str] = []
    for entry in report:
        if not isinstance(entry, dict):
            continue
        file_path = Path(str(entry.get("filePath", "")))
        try:
            shown = file_path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            shown = str(file_path)
        for message in entry.get("messages") or []:
            if not isinstance(message, dict):
                continue
            line = (
                f"{shown}:{message.get('line', 0)}:{message.get('column', 0)}: "
                f"{message.get('ruleId') or 'eslint'}: {message.get('message', '')}"
            )
            (errors if message.get("severity") == 2 else warnings).append(line)
    return errors, warnings


def _verify_eslint(root: Path, paths: list[str], config: GatesConfig) -> list[VerifierOutcome]:
    """Lint the paths with the project's own ESLint config (JSON formatter)."""
    outcomes: list[VerifierOutcome] = []
    for language, subset in _by_language(root, paths).items():
        for eslint_dir, files in _group(root, subset, lambda f: _eslint_dir(root, f)).items():
            if eslint_dir is None:
                outcomes.append(
                    _outcome(
                        "eslint",
                        language,
                        VerifierStatus.NOT_APPLICABLE,
                        "no eslint config above the file(s)",
                        files,
                    )
                )
                continue
            binary = ts.node_bin(root, eslint_dir, "eslint")
            if binary is None:
                outcomes.append(
                    _outcome(
                        "eslint",
                        language,
                        VerifierStatus.NOT_APPLICABLE,
                        "binary not found: eslint",
                        files,
                    )
                )
                continue
            relative = [ts.relative_posix(root / f, eslint_dir) for f in files]
            result = run_argv(
                [binary, "--format", "json", "--", *relative],
                cwd=eslint_dir,
                timeout_s=min(config.timeout_s, VERIFIER_TIMEOUT_S),
            )
            parsed = _eslint_messages(result.stdout, root)
            if parsed is None:
                status = VerifierStatus.PASS if result.passed else VerifierStatus.FAIL
                outcomes.append(
                    _outcome(
                        "eslint", language, status, "" if result.passed else result.tail, files
                    )
                )
                continue
            errors, warnings = parsed
            if errors:
                outcomes.append(
                    _outcome(
                        "eslint", language, VerifierStatus.FAIL, "\n".join(errors + warnings), files
                    )
                )
            elif warnings:
                outcomes.append(
                    _outcome(
                        "eslint", language, VerifierStatus.ADVISORY, "\n".join(warnings), files
                    )
                )
            else:
                outcomes.append(_outcome("eslint", language, VerifierStatus.PASS, "", files))
    return outcomes


def _by_language(root: Path, paths: list[str]) -> dict[Language, list[str]]:
    """Group paths by detected language."""
    out: dict[Language, list[str]] = {}
    for path in paths:
        out.setdefault(language_of(path, root), []).append(path)
    return out


def _verify_ts_imports(root: Path, paths: list[str], config: GatesConfig) -> list[VerifierOutcome]:
    """Resolve every relative and ``paths``-aliased import of each file."""
    outcomes: list[VerifierOutcome] = []
    for language, subset in _by_language(root, paths).items():
        problems: list[str] = []
        for path in subset:
            file = (root / path).resolve()
            try:
                source = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                problems.append(f"{path}: unreadable: {type(exc).__name__}")
                _log.info("gates.file_unreadable", path=path, error=str(exc)[:200])
                continue
            tsconfig_path = ts.find_tsconfig(root, file)
            tsconfig = None
            if tsconfig_path is not None:
                try:
                    tsconfig = ts.load_tsconfig(tsconfig_path)
                except (OSError, ValueError) as exc:
                    problems.append(f"{path}: tsconfig unreadable ({tsconfig_path.name}): {exc}")
                    _log.info(
                        "gates.tsconfig_unreadable", path=str(tsconfig_path), error=str(exc)[:200]
                    )
                    continue
            for line_no, specifier in ts.import_specifiers(source):
                checked, target = ts.resolve_import(file, specifier, tsconfig)
                if checked and target is None:
                    problems.append(f"{path}:{line_no}: cannot resolve import '{specifier}'")
        status = VerifierStatus.FAIL if problems else VerifierStatus.PASS
        outcomes.append(_outcome("ts_imports", language, status, "\n".join(problems), subset))
    return outcomes


def _verify_jsdoc(root: Path, paths: list[str], config: GatesConfig) -> list[VerifierOutcome]:
    """Report exported symbols with no JSDoc block (advisory, never blocking)."""
    outcomes: list[VerifierOutcome] = []
    for language, subset in _by_language(root, paths).items():
        missing: list[str] = []
        for path in subset:
            try:
                source = (root / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                _log.info("gates.jsdoc_unreadable", path=path, error=type(exc).__name__)
                continue
            missing.extend(
                f"{path}:{line}: export '{name}' has no JSDoc"
                for line, name in ts.undocumented_exports(source)
            )
        status = VerifierStatus.ADVISORY if missing else VerifierStatus.PASS
        outcomes.append(_outcome("jsdoc", language, status, "\n".join(missing), subset))
    return outcomes


class _TolerantLoader(yaml.SafeLoader):
    """A safe loader that accepts unknown tags (GitLab's ``!reference``, CloudFormation's ``!Ref``)."""


def _construct_unknown(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> object:
    """Build a plain value for a node carrying an application-specific tag."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_TolerantLoader.add_multi_constructor("!", _construct_unknown)


def _verify_yaml(root: Path, paths: list[str], config: GatesConfig) -> list[VerifierOutcome]:
    """Parse every YAML document of each file."""
    problems: list[str] = []
    for path in paths:
        try:
            text = (root / path).read_text(encoding="utf-8")
            list(yaml.load_all(text, Loader=_TolerantLoader))
        except yaml.MarkedYAMLError as exc:
            mark = exc.problem_mark
            where = f"{path}:{mark.line + 1}:{mark.column + 1}" if mark is not None else path
            problems.append(f"{where}: {exc.problem or exc}")
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            problems.append(f"{path}: {type(exc).__name__}: {exc}")
    status = VerifierStatus.FAIL if problems else VerifierStatus.PASS
    return [_outcome("yaml_parse", Language.YAML, status, "\n".join(problems), paths)]


_MD_INLINE_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_MD_REF_LINK_RE = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
_MD_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _md_targets(text: str) -> Iterable[tuple[int, str]]:
    """Yield ``(line, target)`` of each link outside fenced code and inline code."""
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        visible = re.sub(r"`[^`]*`", "", line)
        for match in _MD_INLINE_LINK_RE.finditer(visible):
            yield line_no, match.group(1)
        ref = _MD_REF_LINK_RE.match(visible)
        if ref:
            yield line_no, ref.group(1)


def _verify_md_links(root: Path, paths: list[str], config: GatesConfig) -> list[VerifierOutcome]:
    """Check that every relative link and image of each Markdown file exists."""
    problems: list[str] = []
    top = root.resolve()
    for path in paths:
        file = (root / path).resolve()
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{path}: unreadable: {type(exc).__name__}")
            _log.info("gates.file_unreadable", path=path, error=str(exc)[:200])
            continue
        for line_no, target in _md_targets(text):
            if target.startswith("#") or _MD_SCHEME_RE.match(target) or target.startswith("//"):
                continue
            bare = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not bare:
                continue
            resolved = (top / bare.lstrip("/")) if bare.startswith("/") else (file.parent / bare)
            if not resolved.exists():
                problems.append(f"{path}:{line_no}: broken link: {target}")
    status = VerifierStatus.FAIL if problems else VerifierStatus.PASS
    return [_outcome("md_links", Language.MARKDOWN, status, "\n".join(problems), paths)]


VERIFIERS: dict[str, Verifier] = {
    "python_syntax": InProcessVerifier("python_syntax", Language.PYTHON, "Developer step chain"),
    "python_imports": InProcessVerifier("python_imports", Language.PYTHON, "Developer step chain"),
    "python_house_lint": InProcessVerifier(
        "python_house_lint", Language.PYTHON, "Developer step chain + finding verifiers"
    ),
    "python_docstring": InProcessVerifier("python_docstring", Language.PYTHON, "finding verifiers"),
    "tsc": FunctionVerifier("tsc", Language.TYPESCRIPT, _verify_tsc, ("tsc",)),
    "eslint": FunctionVerifier("eslint", Language.TYPESCRIPT, _verify_eslint, ("eslint",)),
    "ts_imports": FunctionVerifier("ts_imports", Language.TYPESCRIPT, _verify_ts_imports),
    "jsdoc": FunctionVerifier("jsdoc", Language.TYPESCRIPT, _verify_jsdoc, advisory=True),
    "bash_n": CommandVerifier("bash_n", Language.SHELL, "bash", ("-n", "{file}")),
    "shellcheck": CommandVerifier(
        "shellcheck", Language.SHELL, "shellcheck", ("--severity=warning", "--format=gcc", "{file}")
    ),
    "yaml_parse": FunctionVerifier("yaml_parse", Language.YAML, _verify_yaml),
    "md_links": FunctionVerifier("md_links", Language.MARKDOWN, _verify_md_links),
    "verilator": CommandVerifier(
        "verilator",
        Language.VERILOG,
        "verilator",
        ("--lint-only", "-Wall", "{file}"),
        advisory_unless_declared=True,
    ),
    "gcc": CommandVerifier(
        "gcc", Language.C, "gcc", ("-fsyntax-only", "{file}"), advisory_unless_declared=True
    ),
}
"""Every verifier by name; the language table references these names."""


def _command_argv(verifier: CommandVerifier, config: GatesConfig) -> list[str]:
    """Return the argv template (binary first), honouring ``[gates].verifier_commands``."""
    override = config.verifier_commands.get(verifier.name)
    if override:
        return shlex.split(override)
    return [verifier.binary, *verifier.argv]


def _run_command_verifier(
    verifier: CommandVerifier, language: Language, root: Path, paths: list[str], config: GatesConfig
) -> list[VerifierOutcome]:
    """Run a :class:`CommandVerifier` once per file."""
    template = _command_argv(verifier, config)
    binary = which(template[0]) if "/" not in template[0] else template[0]
    if binary is None:
        return [
            _outcome(
                verifier.name,
                language,
                VerifierStatus.NOT_APPLICABLE,
                f"binary not found: {template[0]}",
                paths,
            )
        ]
    failures: list[str] = []
    for path in paths:
        argv = [binary, *(arg.replace("{file}", path) for arg in template[1:])]
        result = run_argv(argv, cwd=root, timeout_s=min(config.timeout_s, VERIFIER_TIMEOUT_S))
        if not result.passed:
            failures.append(result.tail)
    status = VerifierStatus.FAIL if failures else VerifierStatus.PASS
    return [_outcome(verifier.name, language, status, "\n".join(failures), paths)]


def _apply_policy(outcome: VerifierOutcome, config: GatesConfig, advisory: bool) -> VerifierOutcome:
    """Downgrade a failure to advisory when the verifier or the config says so."""
    if outcome.status is VerifierStatus.FAIL and (
        advisory or outcome.verifier in config.advisory_verifiers
    ):
        return VerifierOutcome(
            outcome.verifier,
            outcome.language,
            VerifierStatus.ADVISORY,
            outcome.detail,
            outcome.paths,
        )
    return outcome


def run_verifiers(
    root: Path,
    paths: Iterable[str],
    config: GatesConfig,
    *,
    skip_languages: frozenset[Language] = frozenset({Language.PYTHON}),
) -> VerifierReport:
    """Run every applicable verifier over the files a change touched.

    Args:
        root: The project root.
        paths: Repo-relative paths (absent paths — deletions — are ignored).
        config: The project configuration.
        skip_languages: Languages checked elsewhere (Python runs in the
            review chain by default) — neither run nor reported.

    Returns:
        The :class:`VerifierReport`; ``passed`` is False when any
        blocking verifier failed.
    """
    by_language: dict[Language, list[str]] = {}
    for path in dict.fromkeys(paths):
        if not (root / path).is_file():
            continue
        by_language.setdefault(language_of(path, root), []).append(path)
    outcomes: list[VerifierOutcome] = []
    for language, files in sorted(by_language.items()):
        if language in skip_languages or language is Language.OTHER:
            continue
        spec = spec_for(language)
        if language not in config.languages:
            outcomes.append(
                _outcome(
                    "language",
                    language,
                    VerifierStatus.NOT_APPLICABLE,
                    f"language not declared: {language.value}",
                    files,
                )
            )
            continue
        if spec is None or not spec.verifiers:
            outcomes.append(
                _outcome(
                    "language",
                    language,
                    VerifierStatus.NOT_APPLICABLE,
                    f"no verifier for {language.value}",
                    files,
                )
            )
            continue
        for name in spec.verifiers:
            verifier = VERIFIERS[name]
            if name in config.disabled_verifiers:
                outcomes.append(
                    _outcome(
                        name,
                        language,
                        VerifierStatus.NOT_APPLICABLE,
                        "disabled by [gates].disabled_verifiers",
                        files,
                    )
                )
                continue
            if isinstance(verifier, InProcessVerifier):
                continue
            if isinstance(verifier, CommandVerifier):
                produced = _run_command_verifier(verifier, language, root, files, config)
                advisory = (
                    verifier.advisory_unless_declared
                    and verifier.name not in config.verifier_commands
                )
            else:
                produced = verifier.fn(root, files, config)
                advisory = verifier.advisory
            outcomes.extend(_apply_policy(o, config, advisory) for o in produced)
    report = VerifierReport(tuple(outcomes))
    _log.info(
        "gates.verifiers",
        n_paths=sum(len(v) for v in by_language.values()),
        passed=report.passed,
        fail=report.count(VerifierStatus.FAIL),
        not_applicable=report.count(VerifierStatus.NOT_APPLICABLE),
    )
    return report


@dataclass(frozen=True)
class AvailabilityRow:
    """One line of ``repoach doctor --gates``.

    Attributes:
        language: The language.
        extensions: Its extensions.
        declared: Whether the project declares it.
        verifiers: ``(name, availability)`` pairs.
        selector_kind: Its test-selector form.
    """

    language: Language
    extensions: tuple[str, ...]
    declared: bool
    verifiers: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    selector_kind: str = ""


def _package_dirs(root: Path) -> list[Path]:
    """Return the root and its first-level subdirectories holding a ``package.json``."""
    dirs = [root]
    dirs.extend(sorted(p.parent for p in root.glob("*/package.json")))
    return dirs


def _availability(name: str, root: Path, config: GatesConfig) -> str:
    """Describe whether verifier ``name`` can run here."""
    if name in config.disabled_verifiers:
        return "disabled"
    verifier = VERIFIERS[name]
    if isinstance(verifier, InProcessVerifier):
        return f"builtin ({verifier.where})"
    if isinstance(verifier, CommandVerifier):
        binary = _command_argv(verifier, config)[0]
        found = which(binary)
        state = f"found: {found}" if found else f"missing: {binary}"
        if verifier.advisory_unless_declared and name not in config.verifier_commands:
            state += " (advisory until declared in [gates].verifier_commands)"
        return state
    states: list[str] = []
    for binary in verifier.binaries:
        found = next((b for d in _package_dirs(root) if (b := ts.node_bin(root, d, binary))), None)
        states.append(f"found: {found}" if found else f"missing: {binary}")
    kind = "advisory" if verifier.advisory else "builtin"
    return ", ".join(states) if states else kind


def availability_table(root: Path, config: GatesConfig) -> list[AvailabilityRow]:
    """Return the per-language verifier availability for ``doctor --gates``."""
    rows: list[AvailabilityRow] = []
    for spec in LANGUAGE_TABLE:
        verifiers = tuple((name, _availability(name, root, config)) for name in spec.verifiers)
        rows.append(
            AvailabilityRow(
                language=spec.language,
                extensions=spec.extensions,
                declared=spec.language in config.languages,
                verifiers=verifiers,
                selector_kind=spec.selector_kind,
            )
        )
    return rows


__all__ = [
    "VERIFIERS",
    "AvailabilityRow",
    "CommandVerifier",
    "FunctionVerifier",
    "InProcessVerifier",
    "VerifierOutcome",
    "VerifierReport",
    "VerifierStatus",
    "availability_table",
    "run_verifiers",
]
