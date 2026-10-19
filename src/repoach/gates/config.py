"""The gate knobs a project declares, read through one small dataclass (SP-UNIVERSAL-GATES).

Every gate, verifier and jail rule that depends on the host project
reads a :class:`GatesConfig`.  The mapping it is built from has the
shape of ``repoach.toml``'s tables (``[project]``, ``[commands]``,
``[gates]``, ``[arch]``, ``[coder]``); this module is the only place
that reads the file, so the project-config loader of
SP-UNIVERSAL-PROJECT-CONFIG replaces :func:`load_gates_config` with one
adapter call (``GatesConfig.from_mapping(project_config.as_mapping())``).

Two defaults coexist on purpose:

* :meth:`GatesConfig.builtin` — no ``repoach.toml`` at all.  It encodes
  repoach's own behaviour before this spec (Python only, the literal
  ``ruff``/``pytest`` gates, docstring exemptions, frontmatter required),
  so a checkout without configuration behaves exactly as it did.
  ``lint_cmd``/``test_cmd`` are ``None`` there, which the callers read
  as "run the built-in Python gate".
* :meth:`GatesConfig.from_mapping` — a project that wrote a
  configuration.  A command it leaves out is ``""`` and its gate is
  skipped with a printed reason; nothing Python-shaped is assumed.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from ..core.paths import NotARepositoryError, is_repoach_checkout, project_root
from .languages import Language, python_applicable

_log = get_logger(__name__)

CONFIG_FILE_NAME = "repoach.toml"
"""The committed, secret-free project file at the host repository root."""

DEFAULT_TIMEOUT_S = 900
"""Seconds a project command may run before it is killed (``[commands].timeout_s``)."""

REPOACH_DOCSTRING_EXEMPT: tuple[str, ...] = ("tests/", "scripts/", "src/repoach/llm_proxy/")
"""Paths repoach's own pyproject exempts from docstring (ruff ``D``) rules."""

DEFAULT_PROMPTS_DIR = "prompts/review"
"""Where the reviewer personas live; always forbidden to the Coder."""


class GatesConfigError(ValueError):
    """A ``repoach.toml`` that cannot be read as gate configuration.

    The message names the file and the dotted key at fault
    (``repoach.toml: project.languages: unknown language 'cobol' …``);
    the CLI reports it as a usage error (rc 2), never a traceback.
    """


def _str_tuple(value: object, key: str) -> tuple[str, ...]:
    """Coerce a TOML array of strings to a tuple, refusing any other shape.

    Args:
        value: The raw TOML value (``None`` when the key is absent).
        key: Dotted key name, for the error message.

    Returns:
        The strings in declaration order; empty when absent.

    Raises:
        ValueError: The value is not an array of strings.
    """
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be an array of strings, got {value!r}")
    return tuple(value)


def _str_value(value: object, key: str) -> str:
    """Coerce an optional TOML string, refusing any other type."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {value!r}")
    return value


def _table(mapping: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Return the sub-table ``name`` of ``mapping`` (empty when absent)."""
    value = mapping.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"[{name}] must be a table, got {value!r}")
    return value


def _named_commands(commands: Mapping[str, Any]) -> dict[str, str]:
    """Return the non-empty command strings of ``[commands]`` by key.

    Raises:
        ValueError: A key other than ``timeout_s`` holds a non-string.
    """
    out: dict[str, str] = {}
    for key, value in commands.items():
        if key == "timeout_s":
            continue
        if not isinstance(value, str):
            raise ValueError(f"commands.{key} must be a string, got {value!r}")
        if value.strip():
            out[str(key)] = value
    return out


def _languages(values: tuple[str, ...]) -> frozenset[Language]:
    """Parse ``[project].languages`` into :class:`Language` members."""
    out: set[Language] = set()
    for raw in values:
        try:
            out.add(Language(raw.strip().lower()))
        except ValueError as exc:
            known = ", ".join(member.value for member in Language)
            raise ValueError(
                f"project.languages: unknown language {raw!r} (known: {known})"
            ) from exc
    return frozenset(out)


@dataclass(frozen=True)
class GatesConfig:
    """The project knobs every gate reads.

    Attributes:
        languages: Languages the project declares; verifiers for any
            other language answer ``not_applicable``.
        src_roots: Source roots (repo-relative); Python verifiers only
            run on ``.py`` files under these or ``test_roots``.
        test_roots: Test roots (repo-relative).
        lint_cmd: ``[commands].lint``; ``None`` runs the built-in ruff
            gate, ``""`` skips the gate.
        test_cmd: ``[commands].test``; ``None`` runs the built-in
            ``pytest -q tests/unit``, ``""`` skips.  ``{python}`` and
            ``{target}`` placeholders are expanded when present.
        gate_cmd: ``[commands].gate``, the long-running project gate;
            ``""`` (or ``None``) means none.
        timeout_s: Kill a project command after this many seconds.
        docstring_exempt: Path prefixes exempt from missing-docstring claims.
        require_frontmatter: Whether a new spec without frontmatter is a
            blocking ``SpecPresenceViolation``.
        forbidden_prefixes: Extra Coder-jail prefixes.
        forbidden_paths: Extra Coder-jail exact paths.
        prompts_dir: Directory of reviewer prompts (always jailed).
        advisory_verifiers: Verifier names whose failures are reported
            but never turn a gate red.
        disabled_verifiers: Verifier names never run.
        named_commands: Every command string of ``[commands]`` by key
            (``lint``, ``test``, ``gate`` and any other the project
            declares) — the only commands a ``cmd:<name>`` test selector
            may run.
        verifier_commands: Per-verifier argv template overrides
            (``{file}`` is the repo-relative path).
        builtin: True for :meth:`builtin` (no configuration file).
    """

    languages: frozenset[Language] = frozenset({Language.PYTHON})
    src_roots: tuple[str, ...] = ("src",)
    test_roots: tuple[str, ...] = ("tests",)
    lint_cmd: str | None = None
    test_cmd: str | None = None
    gate_cmd: str | None = None
    timeout_s: int = DEFAULT_TIMEOUT_S
    docstring_exempt: tuple[str, ...] = REPOACH_DOCSTRING_EXEMPT
    require_frontmatter: bool = True
    forbidden_prefixes: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    prompts_dir: str = DEFAULT_PROMPTS_DIR
    advisory_verifiers: frozenset[str] = frozenset()
    disabled_verifiers: frozenset[str] = frozenset()
    named_commands: Mapping[str, str] = field(default_factory=dict)
    verifier_commands: Mapping[str, str] = field(default_factory=dict)
    builtin: bool = True

    @classmethod
    def builtin_default(cls, *, repoach_project: bool = True) -> GatesConfig:
        """Return the configuration of a project with no ``repoach.toml``.

        Reproduces repoach's behaviour before SP-UNIVERSAL-GATES: Python
        only, built-in ruff and pytest gates, Python verifiers on every
        ``.py`` path (no root restriction).  Spec frontmatter is required
        only in repoach's own checkout; a foreign repository without a
        configuration file has not adopted architecture governance
        (SP-UNIVERSAL-GATES G6).

        Args:
            repoach_project: Whether the host is repoach itself.
        """
        return cls(src_roots=(), test_roots=(), require_frontmatter=repoach_project)

    @classmethod
    def from_mapping(
        cls, mapping: Mapping[str, Any], *, repoach_project: bool = False
    ) -> GatesConfig:
        """Build the configuration from a ``repoach.toml``-shaped mapping.

        Args:
            mapping: The parsed document (``[project]``, ``[commands]``,
                ``[gates]``, ``[arch]``, ``[coder]`` tables; all optional).
            repoach_project: Whether the host is repoach itself; only
                changes the ``docstring_exempt`` and
                ``require_frontmatter`` defaults.

        Returns:
            The configuration.

        Raises:
            ValueError: A key has the wrong type or names an unknown language.
        """
        project = _table(mapping, "project")
        commands = _table(mapping, "commands")
        gates = _table(mapping, "gates")
        arch = _table(mapping, "arch")
        coder = _table(mapping, "coder")
        languages = _languages(_str_tuple(project.get("languages"), "project.languages"))
        src_roots = _str_tuple(project.get("src_roots"), "project.src_roots")
        test_roots = _str_tuple(project.get("test_roots"), "project.test_roots")
        exempt_raw = gates.get("docstring_exempt")
        if exempt_raw is not None:
            exempt = _str_tuple(exempt_raw, "gates.docstring_exempt")
        elif repoach_project:
            exempt = REPOACH_DOCSTRING_EXEMPT
        else:
            exempt = tuple(root.rstrip("/") + "/" for root in test_roots)
        require_raw = arch.get("require_frontmatter", repoach_project)
        if not isinstance(require_raw, bool):
            raise ValueError(f"arch.require_frontmatter must be a boolean, got {require_raw!r}")
        timeout_raw = commands.get("timeout_s", DEFAULT_TIMEOUT_S)
        if not isinstance(timeout_raw, int) or isinstance(timeout_raw, bool) or timeout_raw <= 0:
            raise ValueError(f"commands.timeout_s must be a positive integer, got {timeout_raw!r}")
        overrides_raw = gates.get("verifier_commands", {})
        if not isinstance(overrides_raw, Mapping) or not all(
            isinstance(value, str) for value in overrides_raw.values()
        ):
            raise ValueError("gates.verifier_commands must map verifier names to strings")
        return cls(
            languages=languages,
            src_roots=src_roots,
            test_roots=test_roots,
            lint_cmd=_str_value(commands.get("lint"), "commands.lint"),
            test_cmd=_str_value(commands.get("test"), "commands.test"),
            gate_cmd=_str_value(commands.get("gate"), "commands.gate"),
            timeout_s=timeout_raw,
            docstring_exempt=exempt,
            require_frontmatter=require_raw,
            forbidden_prefixes=_str_tuple(
                coder.get("forbidden_prefixes"), "coder.forbidden_prefixes"
            ),
            forbidden_paths=_str_tuple(coder.get("forbidden_paths"), "coder.forbidden_paths"),
            prompts_dir=_str_value(project.get("prompts_dir"), "project.prompts_dir")
            or DEFAULT_PROMPTS_DIR,
            advisory_verifiers=frozenset(
                _str_tuple(gates.get("advisory_verifiers"), "gates.advisory_verifiers")
            ),
            disabled_verifiers=frozenset(
                _str_tuple(gates.get("disabled_verifiers"), "gates.disabled_verifiers")
            ),
            named_commands=_named_commands(commands),
            verifier_commands=dict(overrides_raw),
            builtin=False,
        )

    @classmethod
    def from_toml_path(cls, path: Path, *, repoach_project: bool = False) -> GatesConfig:
        """Read ``path`` (a ``repoach.toml``) and build the configuration.

        Args:
            path: The TOML file.
            repoach_project: Forwarded to :meth:`from_mapping`.

        Returns:
            The configuration.

        Raises:
            GatesConfigError: The file is unreadable, not valid TOML, or a
                key is malformed; the message names the file and the key.
        """
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise GatesConfigError(f"{path}: invalid TOML: {exc}") from exc
        try:
            return cls.from_mapping(data, repoach_project=repoach_project)
        except ValueError as exc:
            raise GatesConfigError(f"{path}: {exc}") from exc

    @property
    def python_roots(self) -> tuple[str, ...]:
        """Roots a ``.py`` path must sit under for Python verifiers; empty = anywhere."""
        return self.src_roots + self.test_roots

    @property
    def command_mode(self) -> bool:
        """Whether the gates run the project's declared commands (a config file exists)."""
        return not self.builtin


def applicable(path: str, config: GatesConfig) -> tuple[bool, str]:
    """Decide whether a Python-only verifier may run on ``path`` under ``config``.

    Args:
        path: Repo-relative path the verifier would read.
        config: The project configuration.

    Returns:
        ``(True, "")`` or ``(False, reason)``; see
        :func:`repoach.gates.languages.python_applicable`.
    """
    return python_applicable(path, config.languages, config.python_roots)


def load_gates_config(root: Path) -> GatesConfig:
    """Return the gate configuration of the project at ``root``.

    Args:
        root: The project root.

    Returns:
        :meth:`GatesConfig.from_toml_path` when ``root/repoach.toml``
        exists, else :meth:`GatesConfig.builtin_default`.

    Raises:
        GatesConfigError: ``repoach.toml`` exists but is malformed.
    """
    path = root / CONFIG_FILE_NAME
    if not path.is_file():
        return GatesConfig.builtin_default(repoach_project=is_repoach_checkout(root))
    return GatesConfig.from_toml_path(path, repoach_project=is_repoach_checkout(root))


def current_gates_config() -> GatesConfig:
    """Return the configuration of the project the working directory sits in.

    Outside any git working tree, or without ``git``, the built-in
    default applies (logged).
    """
    try:
        root = project_root()
    except NotARepositoryError as exc:
        _log.info("gates.config_no_project", error=str(exc)[:200])
        return GatesConfig.builtin_default()
    return load_gates_config(root)


__all__ = [
    "CONFIG_FILE_NAME",
    "DEFAULT_TIMEOUT_S",
    "GatesConfig",
    "GatesConfigError",
    "applicable",
    "current_gates_config",
    "load_gates_config",
    "python_packages",
]


def python_packages(root: Path, config: GatesConfig) -> dict[str, Path]:
    """Return the top-level Python packages of the project and the directory holding each.

    Each source root contributes its child directories that carry an
    ``__init__.py`` (``src/repoach`` → ``repoach`` under ``src``), and
    the root itself when it is a package (``api/__init__.py`` →
    ``api`` under the repository root).  The built-in configuration (no
    ``repoach.toml``) keeps repoach's historical answer verbatim:
    ``repoach`` under ``src``, whether or not it exists on disk.

    Args:
        root: The project root.
        config: The project configuration.

    Returns:
        ``{top_package: absolute directory the package sits in}``.
    """
    if config.builtin:
        return {"repoach": (root / "src").resolve()}
    packages: dict[str, Path] = {}
    for src_root in config.src_roots:
        base = (root / src_root).resolve()
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "__init__.py").is_file():
                packages.setdefault(child.name, base)
        if (base / "__init__.py").is_file() and base != root.resolve():
            packages.setdefault(base.name, base.parent)
    return packages
