"""The gate knobs a project declares, read through one small dataclass (SP-UNIVERSAL-GATES).

Every gate, verifier and jail rule that depends on the host project
reads a :class:`GatesConfig`.  ``repoach.toml`` has one reader,
:func:`repoach.project.config.load_project_config`
(SP-UNIVERSAL-PROJECT-CONFIG); :func:`load_gates_config` adapts its
host-resolved result with one call,
``GatesConfig.from_mapping(project_config.as_mapping())``, and a
malformed file or a gate key the project config does not check
(``[project].languages``) is the same
:class:`~repoach.project.config.ConfigError`.

Two shapes coexist on purpose:

* no ``repoach.toml`` at all — :meth:`GatesConfig.builtin_default`
  encodes repoach's behaviour before this spec (Python only, the
  built-in ``ruff``/``pytest`` gates, Python verifiers on every ``.py``
  path), so a checkout without configuration behaves exactly as it
  did.  ``lint_cmd``/``test_cmd`` are ``None`` there, which the callers
  read as "run the built-in Python gate".
* a project that wrote a configuration — :meth:`GatesConfig.from_mapping`.
  A command it leaves out (and the host does not default) is ``""``
  and its gate is skipped with a printed reason; nothing Python-shaped
  is assumed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import configured_env_value
from ..core.logging import get_logger
from ..core.paths import NotARepositoryError, is_repoach_checkout, project_root
from ..project.config import (
    CONFIG_FILE_NAME,
    DEFAULT_PROMPTS_DIR,
    DEFAULT_REFUTER_MAX_JUDGED,
    DEFAULT_TIMEOUT_S,
    REFUTER_CONTEXT_TOKENS_FLOOR,
    REPOACH_DOCSTRING_EXEMPT,
    ConfigError,
    config_path,
    load_project_config,
)
from .languages import Language, python_applicable

_log = get_logger(__name__)

GatesConfigError = ConfigError
"""A gate configuration problem is a project configuration problem: one
error type, one bootstrap check, file and key named in the message."""


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
        raise ValueError(f"{key}: must be an array of strings, got {value!r}")
    return tuple(value)


def _str_value(value: object, key: str) -> str:
    """Coerce an optional TOML string, refusing any other type."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key}: must be a string, got {value!r}")
    return value


def _table(mapping: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Return the sub-table ``name`` of ``mapping`` (empty when absent)."""
    value = mapping.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{name}: must be a table, got {value!r}")
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
            raise ValueError(f"commands.{key}: must be a string, got {value!r}")
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
        refuter_max_judged: ``[gates].refuter_max_judged``, the findings
            the refuter sends to its judge per PR; the overflow is
            reported deferred, each finding named.
        refuter_context_tokens: ``[gates].refuter_context_tokens``, the
            refuter's context size its evidence fills, at least
            :data:`REFUTER_CONTEXT_TOKENS_FLOOR`.
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
    refuter_max_judged: int = DEFAULT_REFUTER_MAX_JUDGED
    refuter_context_tokens: int = REFUTER_CONTEXT_TOKENS_FLOOR
    builtin: bool = True

    @classmethod
    def builtin_default(cls, *, require_frontmatter: bool = True) -> GatesConfig:
        """Return the configuration of a project with no ``repoach.toml``.

        Reproduces repoach's behaviour before SP-UNIVERSAL-GATES: Python
        only, built-in ruff and pytest gates, Python verifiers on every
        ``.py`` path (no root restriction).

        Args:
            require_frontmatter: The host default from
                :meth:`repoach.project.config.ProjectConfig.with_host_defaults`
                (repoach itself: true; a foreign repository: false,
                SP-UNIVERSAL-GATES G6).
        """
        return cls(src_roots=(), test_roots=(), require_frontmatter=require_frontmatter)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> GatesConfig:
        """Build the configuration from a ``repoach.toml``-shaped mapping.

        Feed it ``ProjectConfig.with_host_defaults(...).as_mapping()``:
        the host-dependent defaults (``docstring_exempt``,
        ``require_frontmatter``, repoach's own lint/test commands) are
        already filled there.  A key still absent falls back to the
        foreign-repository default (test roots exempt, frontmatter not
        required, command skipped).

        Args:
            mapping: The document (``[project]``, ``[commands]``,
                ``[gates]``, ``[arch]``, ``[coder]`` tables; all optional).

        Returns:
            The configuration.

        Raises:
            ValueError: A key has the wrong type or names an unknown
                language; the message starts with the dotted key.
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
        else:
            exempt = tuple(root.rstrip("/") + "/" for root in test_roots)
        require_raw = arch.get("require_frontmatter", False)
        if not isinstance(require_raw, bool):
            raise ValueError(f"arch.require_frontmatter: must be a boolean, got {require_raw!r}")
        timeout_raw = commands.get("timeout_s", DEFAULT_TIMEOUT_S)
        if not isinstance(timeout_raw, int) or isinstance(timeout_raw, bool) or timeout_raw <= 0:
            raise ValueError(f"commands.timeout_s: must be a positive integer, got {timeout_raw!r}")
        overrides_raw = gates.get("verifier_commands", {})
        if not isinstance(overrides_raw, Mapping) or not all(
            isinstance(value, str) for value in overrides_raw.values()
        ):
            raise ValueError("gates.verifier_commands: must map verifier names to strings")
        max_judged = gates.get("refuter_max_judged", DEFAULT_REFUTER_MAX_JUDGED)
        if not isinstance(max_judged, int) or isinstance(max_judged, bool) or max_judged <= 0:
            raise ValueError(
                f"gates.refuter_max_judged: must be a positive integer, got {max_judged!r}"
            )
        context_tokens = gates.get("refuter_context_tokens", REFUTER_CONTEXT_TOKENS_FLOOR)
        if (
            not isinstance(context_tokens, int)
            or isinstance(context_tokens, bool)
            or context_tokens < REFUTER_CONTEXT_TOKENS_FLOOR
        ):
            raise ValueError(
                "gates.refuter_context_tokens: must be an integer of at least "
                f"{REFUTER_CONTEXT_TOKENS_FLOOR}, got {context_tokens!r}"
            )
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
            refuter_max_judged=max_judged,
            refuter_context_tokens=context_tokens,
            builtin=False,
        )

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

    Reads ``repoach.toml`` through
    :func:`repoach.project.config.load_project_config` (never directly),
    applies the host defaults, and adapts the mapping.

    Args:
        root: The project root.

    Returns:
        :meth:`GatesConfig.from_mapping` of the resolved configuration
        when ``root/repoach.toml`` exists, else
        :meth:`GatesConfig.builtin_default` with the host's frontmatter
        default.

    Raises:
        ConfigError: ``repoach.toml`` is malformed, or a gate key holds a
            value the gates refuse (an unknown language); the message
            names the file and the key.
    """
    project = load_project_config(root).with_host_defaults(repoach_host=is_repoach_checkout(root))
    path = config_path(root)
    if not path.is_file():
        return GatesConfig.builtin_default(
            require_frontmatter=bool(project.arch.require_frontmatter)
        )
    try:
        return GatesConfig.from_mapping(project.as_mapping())
    except ValueError as exc:
        key, _, reason = str(exc).partition(": ")
        raise ConfigError(path, reason or str(exc), key=key if reason else "") from exc


MAX_JUDGED_ENV = "REPOACH_REFUTER_MAX_JUDGED"
"""Environment override of ``[gates].refuter_max_judged`` (env > toml > 10)."""


def resolve_refuter_max_judged(repo_root: Path) -> tuple[int, str]:
    """Return the refuter cap for the project at ``repo_root`` and where it came from.

    :data:`MAX_JUDGED_ENV` (a positive integer) wins, then
    ``[gates].refuter_max_judged``, then
    :data:`DEFAULT_REFUTER_MAX_JUDGED`. An invalid
    environment value or a malformed ``repoach.toml`` is logged at
    warning and skipped: the refuter never fails a review over its cap.

    Returns:
        ``(cap, source)`` with ``source`` one of ``env
        REPOACH_REFUTER_MAX_JUDGED``, ``repoach.toml`` or ``default``.
    """
    raw = configured_env_value((MAX_JUDGED_ENV,), repo_root)
    if raw is not None:
        value = raw.strip()
        if value.isdigit() and int(value) > 0:
            return int(value), f"env {MAX_JUDGED_ENV}"
        _log.warning(
            "gates.refuter_max_judged_env_invalid", variable=MAX_JUDGED_ENV, value=raw[:40]
        )
    try:
        config = load_gates_config(repo_root)
    except GatesConfigError as exc:
        _log.warning(
            "gates.refuter_max_judged_config_invalid",
            error=str(exc)[:200],
            fallback=DEFAULT_REFUTER_MAX_JUDGED,
        )
        return DEFAULT_REFUTER_MAX_JUDGED, "default"
    if config.builtin or config.refuter_max_judged == DEFAULT_REFUTER_MAX_JUDGED:
        return config.refuter_max_judged, "default"
    return config.refuter_max_judged, "repoach.toml"


CONTEXT_TOKENS_ENV = "REPOACH_REFUTER_CONTEXT_TOKENS"
"""Environment override of ``[gates].refuter_context_tokens`` (env > toml > 40000)."""


def resolve_refuter_context_tokens(repo_root: Path) -> tuple[int, str]:
    """Return the refuter's context size for the project at ``repo_root`` and its source.

    :data:`CONTEXT_TOKENS_ENV` (an integer of at least
    :data:`REFUTER_CONTEXT_TOKENS_FLOOR`) wins, then
    ``[gates].refuter_context_tokens``, then the floor itself. An invalid
    environment value or a malformed ``repoach.toml`` is logged at
    warning and skipped: the refuter never fails a review over its size.

    Returns:
        ``(tokens, source)`` with ``source`` one of ``env
        REPOACH_REFUTER_CONTEXT_TOKENS``, ``repoach.toml`` or ``default``.
    """
    raw = configured_env_value((CONTEXT_TOKENS_ENV,), repo_root)
    if raw is not None:
        value = raw.strip()
        if value.isdigit() and int(value) >= REFUTER_CONTEXT_TOKENS_FLOOR:
            return int(value), f"env {CONTEXT_TOKENS_ENV}"
        _log.warning(
            "gates.refuter_context_tokens_env_invalid",
            variable=CONTEXT_TOKENS_ENV,
            value=raw[:40],
            floor=REFUTER_CONTEXT_TOKENS_FLOOR,
        )
    try:
        config = load_gates_config(repo_root)
    except GatesConfigError as exc:
        _log.warning(
            "gates.refuter_context_tokens_config_invalid",
            error=str(exc)[:200],
            fallback=REFUTER_CONTEXT_TOKENS_FLOOR,
        )
        return REFUTER_CONTEXT_TOKENS_FLOOR, "default"
    if config.builtin or config.refuter_context_tokens == REFUTER_CONTEXT_TOKENS_FLOOR:
        return config.refuter_context_tokens, "default"
    return config.refuter_context_tokens, "repoach.toml"


def current_gates_config() -> GatesConfig:
    """Return the configuration of the project the working directory sits in.

    Outside any git working tree, or without ``git``, the built-in
    default applies (logged).
    """
    try:
        root = project_root()
    except NotARepositoryError as exc:
        _log.info("gates.config_no_project", error=str(exc)[:200])
        return GatesConfig.builtin_default(require_frontmatter=False)
    return load_gates_config(root)


__all__ = [
    "CONFIG_FILE_NAME",
    "CONTEXT_TOKENS_ENV",
    "DEFAULT_PROMPTS_DIR",
    "DEFAULT_REFUTER_MAX_JUDGED",
    "DEFAULT_TIMEOUT_S",
    "MAX_JUDGED_ENV",
    "REFUTER_CONTEXT_TOKENS_FLOOR",
    "REPOACH_DOCSTRING_EXEMPT",
    "GatesConfig",
    "GatesConfigError",
    "applicable",
    "current_gates_config",
    "load_gates_config",
    "python_packages",
    "resolve_refuter_context_tokens",
    "resolve_refuter_max_judged",
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
