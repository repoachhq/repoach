"""The ``repoach.toml`` file model and its loader (SP-UNIVERSAL-PROJECT-CONFIG G1).

One committed, secret-free file at the host repository root carries
every project-specific fact repoach needs.  Every key is optional and
every default equals repoach's own behaviour on 2026-10-18, so a
repository without the file behaves exactly as repoach did before the
file existed (the golden test in ``tests/unit/test_project_config.py``
pins each default against the literal it replaces).

Validation is strict: an unknown key, a value of the wrong type, a key
whose name looks like a secret (``*_key``, ``*_token``, ``*_secret``), a
path that leaves the repository, or a ``schema`` newer than this
repoach raises :class:`ConfigError`, whose message names the file, the
dotted key and, when it can be found, the line.

This module reads the file only.  The environment layer and the origin
remote are applied by :mod:`repoach.project.resolve`; the
:class:`~repoach.core.config.Settings` singleton reads the file through
:func:`settings_file_values` as a source placed below the environment.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

CONFIG_FILE_NAME = "repoach.toml"
"""File name of the project configuration, at the git toplevel."""

SCHEMA_VERSION = 1
"""Highest ``[repoach].schema`` this repoach understands."""

DEFAULT_SPECS_DIR = "docs/specs"
"""Spec directory before the file existed (``review/spec.py`` ``SPECS_DIR``)."""

DEFAULT_PLANS_DIR = "docs/plans"
"""Plan directory before the file existed (``review/plan.py`` ``plan_relpath``)."""

DEFAULT_LINT_COMMAND = "ruff check src tests && ruff format --check src tests"
"""The Coder's lint gate before the file existed (``coder_loop.run_ruff_gate``)."""

DEFAULT_TEST_COMMAND = "pytest -q tests/unit"
"""The Coder's test gate before the file existed (``coder_loop.run_pytest``)."""

DEFAULT_TIMEOUT_S = 900
"""Seconds a project command may run (``coder_loop`` ran pytest with this budget)."""

DEFAULT_REFUTER_MAX_JUDGED = 10
"""Findings the refuter judges per PR when ``[gates].refuter_max_judged`` is unset."""

REFUTER_CONTEXT_TOKENS_FLOOR = 40_000
"""The refuter's context size when ``[gates].refuter_context_tokens`` is unset, and its floor.

The refuter's own ``context_tokens`` of SP-AGENT-CONTEXT-LOOP D1 (owner
2026-10-25): the window must be large enough for the refuter to do its
job, so a smaller value is refused."""

DEFAULT_PROMPTS_DIR = "prompts/review"
"""Where the reviewer personas live; always forbidden to the Coder."""

REPOACH_DOCSTRING_EXEMPT: tuple[str, ...] = ("tests/", "scripts/", "src/repoach/llm_proxy/")
"""Paths repoach's own pyproject exempts from docstring rules (its host default)."""

SECRET_SUFFIXES: tuple[str, ...] = ("_key", "_token", "_secret")
"""Key-name suffixes refused anywhere in the file (NG1: no secrets, ever)."""

SpecSource = Literal["flag", "description", "branch"]
"""Where a pull request's spec id may come from, in resolution order."""

DEFAULT_SPEC_SOURCES: tuple[SpecSource, ...] = ("flag", "description", "branch")
"""Default spec resolution order (G4)."""

ForgeName = Literal["github", "gitlab"]
MergeMethodName = Literal["squash", "merge", "rebase"]
ProviderKind = Literal[
    "",
    "anthropic",
    "claude_code",
    "openai_compatible",
    "gemini",
    "gateway",
    "openai",
    "ollama",
    "openrouter",
    "groq",
    "deepseek",
    "mistral",
]
"""A provider kind, or a preset naming an ``openai_compatible`` endpoint (SP-UNIVERSAL-PROVIDERS)."""

_KEY_LINE_RE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_-]+)\s*=")
_SECTION_LINE_RE = re.compile(r"^\s*\[\s*(?P<name>[A-Za-z0-9_.-]+)\s*\]\s*$")

_DECODE_POSITION_RE = re.compile(r"\(at line (?P<line>\d+), column \d+\)")
"""Position suffix ``tomllib`` puts in its messages on every supported Python."""


def _decode_error_line(exc: ValueError) -> int | None:
    """Return the 1-based line of a TOML parse error, when ``tomllib`` says it.

    Python 3.14 exposes ``lineno``; 3.11 to 3.13 only write the position
    into the message (``Invalid value (at line 2, column 9)``).

    Args:
        exc: The parse error (a ``tomllib.TOMLDecodeError``).

    Returns:
        The line, or None when neither the attribute nor the message
        carries one (an error at end of document, for instance).
    """
    lineno = getattr(exc, "lineno", None)
    if isinstance(lineno, int):
        return lineno
    match = _DECODE_POSITION_RE.search(str(exc))
    return int(match.group("line")) if match is not None else None


class ConfigError(ValueError):
    """``repoach.toml`` is unreadable or invalid.

    Attributes:
        path: The file that was read.
        key: Dotted key the problem is about (``repoach.merge_method``),
            empty for a whole-file problem.
        line: 1-based line of the key or of the parse error, when known.
        reason: The problem, without the location prefix.
    """

    def __init__(self, path: Path, reason: str, *, key: str = "", line: int | None = None) -> None:
        self.path = path
        self.key = key
        self.line = line
        self.reason = reason
        where = f"{path}:{line}" if line is not None else str(path)
        subject = f" key {key!r}:" if key else ""
        super().__init__(f"{where}:{subject} {reason}")


class _Section(BaseModel):
    """Base of every section: frozen, unknown keys refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _relative_inside(value: str) -> str:
    """Refuse a path that is absolute, home-relative or climbs out of the repository.

    A leading ``~`` is refused like an absolute path: the shell, and
    ``Path.expanduser`` downstream, would turn it into one.

    Args:
        value: A repository-relative path as written in the file.

    Returns:
        The value unchanged.

    Raises:
        ValueError: The path is absolute, starts with ``~`` or has a
            ``..`` segment.
    """
    if not value:
        return value
    posix = PurePosixPath(value.replace("\\", "/"))
    if (
        posix.is_absolute()
        or Path(value).is_absolute()
        or value.startswith("~")
        or ".." in posix.parts
    ):
        raise ValueError(
            f"{value!r} must be a path relative to the repository root, without '~' or '..'"
        )
    return value


class RepoachSection(_Section):
    """``[repoach]``: forge, branches, merge method, spec and plan directories."""

    schema_: int = Field(default=SCHEMA_VERSION, alias="schema", ge=1)
    forge: ForgeName | None = None
    integration_branch: str = Field(default="develop", min_length=1)
    release_branch: str = Field(default="main", min_length=1)
    merge_method: MergeMethodName = "squash"
    remove_source_branch: bool = True
    specs_dir: str = Field(default=DEFAULT_SPECS_DIR, min_length=1)
    plans_dir: str = Field(default=DEFAULT_PLANS_DIR, min_length=1)

    @field_validator("specs_dir", "plans_dir")
    @classmethod
    def _check_dirs(cls, value: str) -> str:
        """Keep the spec and plan directories inside the repository."""
        return _relative_inside(value)


class ProjectSection(_Section):
    """``[project]``: measured languages and layout (consumed from M4/M5 on)."""

    languages: list[str] = Field(default_factory=lambda: ["python"])
    src_roots: list[str] = Field(default_factory=lambda: ["src"])
    test_roots: list[str] = Field(default_factory=lambda: ["tests"])
    prompts_dir: str = Field(default=DEFAULT_PROMPTS_DIR, min_length=1)

    @field_validator("prompts_dir")
    @classmethod
    def _check_prompts_dir(cls, value: str) -> str:
        """Keep the prompts directory inside the repository."""
        return _relative_inside(value)


class CommandsSection(_Section):
    """``[commands]``: the project's own lint, test and gate commands.

    An undeclared ``lint`` / ``test`` (None) means repoach's built-in
    ruff / pytest command only in repoach's own checkout; on a foreign
    repository it stays None and the gate skips it, saying why
    (:meth:`ProjectConfig.with_host_defaults`).  An empty string is a
    declared "no command".

    Besides ``lint``, ``test``, ``gate`` and ``timeout_s``, any other key
    is a named command (``e2e = "npm run e2e"``) that a gate selector
    ``cmd:<name>`` may run; each must be a non-empty string.  A gate
    needing an environment variable spells it with ``env``:
    ``gate = "env YOOB_CI_WAIT=1 ./ci/run-local-ci.sh --affected"``.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    lint: str | None = None
    test: str | None = None
    gate: str = ""
    timeout_s: StrictInt = Field(default=DEFAULT_TIMEOUT_S, gt=0)

    @model_validator(mode="after")
    def _check_named_commands(self) -> CommandsSection:
        """Refuse a named command that is not a non-empty string.

        Raises:
            ValueError: An extra key holds a non-string or a blank string.
        """
        for name, value in (self.model_extra or {}).items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"commands.{name} must be a non-empty command string")
        return self

    def named(self) -> dict[str, str]:
        """Return the named commands (every extra key), in file order."""
        return {name: str(value) for name, value in (self.model_extra or {}).items()}


class CoderSection(_Section):
    """``[coder]``: paths added to the Coder's built-in jail (M5)."""

    forbidden_prefixes: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)


class ProviderSection(_Section):
    """``[provider]``: the provider *name* and model ids, never a credential (M4).

    ``cli_path`` is the ``claude`` executable the ``claude_code`` provider
    runs; empty means ``claude`` on ``PATH`` (SP-UNIVERSAL-PROVIDERS).
    ``base_url`` is the endpoint root of a bare ``openai_compatible``
    (required there); a preset or ``gemini`` fixes its host and refuses it.
    """

    kind: ProviderKind = ""
    opus: str = ""
    sonnet: str = ""
    haiku: str = ""
    cli_path: str = ""
    base_url: str = ""


class ReviewSection(_Section):
    """``[review]``: the bot identity and the spec resolution order."""

    bot_username: str = ""
    spec_sources: list[SpecSource] = Field(default_factory=lambda: list(DEFAULT_SPEC_SOURCES))


class GatesSection(_Section):
    """``[gates]``: verifier knobs (SP-UNIVERSAL-GATES).

    ``docstring_exempt`` None means the host default
    (:meth:`ProjectConfig.with_host_defaults`). ``refuter_max_judged``
    caps the findings the refuter sends to its judge per PR; the rest
    are reported deferred, each one named (SP-REFUTER-JUDGES-WHAT-IT-CAN);
    ``REPOACH_REFUTER_MAX_JUDGED`` outranks it, so the value is coerced
    from the environment's string but never from a boolean.
    ``refuter_context_tokens`` is the refuter's context size, the budget
    its evidence fills, never below :data:`REFUTER_CONTEXT_TOKENS_FLOOR`;
    ``REPOACH_REFUTER_CONTEXT_TOKENS`` outranks it.
    """

    docstring_exempt: list[str] | None = None
    advisory_verifiers: list[str] = Field(default_factory=list)
    disabled_verifiers: list[str] = Field(default_factory=list)
    verifier_commands: dict[str, str] = Field(default_factory=dict)
    refuter_max_judged: int = Field(default=DEFAULT_REFUTER_MAX_JUDGED, gt=0)
    refuter_context_tokens: int = Field(
        default=REFUTER_CONTEXT_TOKENS_FLOOR, ge=REFUTER_CONTEXT_TOKENS_FLOOR
    )

    @field_validator("refuter_max_judged", "refuter_context_tokens", mode="before")
    @classmethod
    def _no_bool_cap(cls, value: object) -> object:
        """Refuse ``true`` / ``false`` for a refuter size (an int, not a switch).

        Raises:
            ValueError: The value is a boolean.
        """
        if isinstance(value, bool):
            raise ValueError("gates refuter sizes must be positive integers, not booleans")
        return value


class ArchSection(_Section):
    """``[arch]``: edge-honesty settings (moved from ``[tool.repoach.arch]``, G6).

    ``require_frontmatter`` None means the host default: required in
    repoach's own checkout, not on a foreign repository.
    """

    frontier_suppress: list[str] = Field(default_factory=list)
    require_frontmatter: StrictBool | None = None


class StateSection(_Section):
    """``[state]``: where the ledger and the diagnostic dumps go.

    Empty means the per-user default
    (``$XDG_DATA_HOME/repoach/<slug>/...``).  A committed file may only
    name a path inside the repository: a clone must never be able to
    point repoach's writes at an arbitrary location.
    """

    db_path: str = ""
    logs_dir: str = ""

    @field_validator("db_path", "logs_dir")
    @classmethod
    def _check_paths(cls, value: str) -> str:
        """Keep the ledger and the dumps inside the repository."""
        return _relative_inside(value)


class ConventionsSection(_Section):
    """``[conventions]``: read and type-checked only; enforced by SP-CONVENTIONS-GATE."""

    types: list[str] = Field(
        default_factory=lambda: [
            "feat",
            "fix",
            "perf",
            "refactor",
            "docs",
            "test",
            "build",
            "ci",
            "chore",
            "style",
            "revert",
        ]
    )
    scopes: str = r"[a-z0-9._/-]+"
    branch_pattern: str = "<type>/<slug>"
    exempt_branches: list[str] = Field(default_factory=lambda: ["main", "develop"])
    pr_title: bool = True
    breaking: bool = True
    english: bool = True
    header_max_chars: int = 72


class ProjectConfig(_Section):
    """The whole ``repoach.toml``; ``ProjectConfig()`` is the no-file behaviour."""

    repoach: RepoachSection = Field(default_factory=RepoachSection)
    project: ProjectSection = Field(default_factory=ProjectSection)
    commands: CommandsSection = Field(default_factory=CommandsSection)
    coder: CoderSection = Field(default_factory=CoderSection)
    provider: ProviderSection = Field(default_factory=ProviderSection)
    review: ReviewSection = Field(default_factory=ReviewSection)
    gates: GatesSection = Field(default_factory=GatesSection)
    arch: ArchSection = Field(default_factory=ArchSection)
    state: StateSection = Field(default_factory=StateSection)
    conventions: ConventionsSection = Field(default_factory=ConventionsSection)

    def is_set(self, dotted: str) -> bool:
        """Return whether the file set ``section.key`` explicitly.

        Args:
            dotted: ``section.key``, e.g. ``repoach.forge``.

        Returns:
            True when the key was present in the file.
        """
        section_name, _, key = dotted.partition(".")
        if section_name not in self.model_fields_set:
            return False
        section = getattr(self, section_name)
        field_name = "schema_" if key == "schema" else key
        return field_name in section.model_fields_set or field_name in (section.model_extra or {})

    def as_mapping(self) -> dict[str, Any]:
        """Return the configuration as a ``repoach.toml``-shaped dict.

        One table per section, keyed as in the file (``schema``, not the
        attribute name); named ``[commands]`` entries pass through; keys
        still None (undeclared, no host default) are left out, exactly
        as if the file omitted them.  Call it on the resolved
        configuration (``resolve_project_config(root).config``) to get
        the effective values: ``GatesConfig.from_mapping(cfg.as_mapping())``
        is the one adapter the gates need. Two values stay as written:
        an empty ``[review].bot_username`` and empty ``[state]`` paths
        mean "the forge / per-user default", expanded only by
        :func:`repoach.project.resolve.effective_rows`.

        Returns:
            A fresh, mutable nested dict of plain values.
        """
        return self.model_dump(by_alias=True, exclude_none=True)

    def with_host_defaults(self, *, repoach_host: bool) -> ProjectConfig:
        """Fill the keys whose default depends on the host repository.

        Args:
            repoach_host: Whether the host is a checkout of repoach itself.

        Returns:
            A copy where ``[gates].docstring_exempt`` and
            ``[arch].require_frontmatter`` hold a value: repoach's own
            exemptions and ``true`` on repoach, the test roots and
            ``false`` on a foreign repository.  An undeclared
            ``[commands].lint`` / ``test`` becomes the built-in ruff /
            pytest command on repoach and stays None (skipped) elsewhere.
        """
        commands = self.commands
        if repoach_host:
            filled = {
                key: default
                for key, default in (("lint", DEFAULT_LINT_COMMAND), ("test", DEFAULT_TEST_COMMAND))
                if getattr(commands, key) is None
            }
            if filled:
                commands = commands.model_copy(update=filled)
        gates = self.gates
        if gates.docstring_exempt is None:
            exempt = (
                list(REPOACH_DOCSTRING_EXEMPT)
                if repoach_host
                else [root.rstrip("/") + "/" for root in self.project.test_roots]
            )
            gates = gates.model_copy(update={"docstring_exempt": exempt})
        arch = self.arch
        if arch.require_frontmatter is None:
            arch = arch.model_copy(update={"require_frontmatter": repoach_host})
        return self.model_copy(update={"commands": commands, "gates": gates, "arch": arch})


def config_path(root: Path) -> Path:
    """Return ``<root>/repoach.toml``.

    Args:
        root: The project root (git toplevel).

    Returns:
        The path, whether or not the file exists.
    """
    return root / CONFIG_FILE_NAME


def _line_of(text: str, section: str, key: str) -> int | None:
    """Return the 1-based line where ``key`` is assigned inside ``[section]``.

    Args:
        text: The file content.
        section: Table name, empty for a top-level key.
        key: Key name.

    Returns:
        The line number, or None when the key is not written literally.
    """
    current = ""
    for number, line in enumerate(text.splitlines(), start=1):
        header = _SECTION_LINE_RE.match(line)
        if header is not None:
            current = header.group("name")
            continue
        match = _KEY_LINE_RE.match(line)
        if match is not None and current == section and match.group("key") == key:
            return number
    return None


def _refuse_secret_keys(path: Path, text: str, data: dict[str, Any], prefix: str = "") -> None:
    """Raise when any key, at any depth, is named like a secret.

    Args:
        path: The file, for the message.
        text: The file content, for the line lookup.
        data: A parsed table.
        prefix: Dotted name of ``data`` inside the file.

    Raises:
        ConfigError: A key ends in one of :data:`SECRET_SUFFIXES`.
    """
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if key.lower().endswith(SECRET_SUFFIXES):
            raise ConfigError(
                path,
                "looks like a secret; repoach.toml is committed, put credentials in "
                "the environment or ~/.config/repoach/env",
                key=dotted,
                line=_line_of(text, prefix, key),
            )
        if isinstance(value, dict):
            _refuse_secret_keys(path, text, value, dotted)


def _dotted_location(loc: tuple[int | str, ...]) -> tuple[str, str, str]:
    """Turn a pydantic error location into ``(dotted, section, key)``.

    Args:
        loc: The error's ``loc`` tuple.

    Returns:
        The dotted key, the table holding it and its own name.
    """
    names = [str(part) for part in loc if isinstance(part, str)]
    names = ["schema" if name == "schema_" else name for name in names]
    if not names:
        return "", "", ""
    if len(names) == 1:
        return names[0], "", names[0]
    return ".".join(names[:2]), names[0], names[1]


def parse_project_config(text: str, path: Path) -> ProjectConfig:
    """Parse and validate the content of a ``repoach.toml``.

    Args:
        text: The file content.
        path: Where it came from, for messages.

    Returns:
        The validated configuration.

    Raises:
        ConfigError: Unparsable TOML, an unknown key, a wrong type, a
            secret-looking key, a path leaving the repository, or a
            ``schema`` newer than :data:`SCHEMA_VERSION`.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, f"not valid TOML: {exc}", line=_decode_error_line(exc)) from exc
    _refuse_secret_keys(path, text, data)
    try:
        config = ProjectConfig.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        dotted, section, key = _dotted_location(tuple(first["loc"]))
        reason = first["msg"]
        if first["type"] == "extra_forbidden":
            reason = "unknown key"
        raise ConfigError(path, reason, key=dotted, line=_line_of(text, section, key)) from exc
    if config.repoach.schema_ > SCHEMA_VERSION:
        raise ConfigError(
            path,
            f"schema {config.repoach.schema_} is newer than this repoach understands "
            f"({SCHEMA_VERSION}); upgrade repoach",
            key="repoach.schema",
            line=_line_of(text, "repoach", "schema"),
        )
    return config


def load_project_config(root: Path) -> ProjectConfig:
    """Load ``<root>/repoach.toml``; defaults when the file is absent.

    Args:
        root: The project root (git toplevel).

    Returns:
        The validated configuration; ``ProjectConfig()`` without a file.

    Raises:
        ConfigError: The file exists but is unreadable or invalid.
    """
    path = config_path(root)
    if not path.is_file():
        return ProjectConfig()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(path, f"unreadable: {exc}") from exc
    return parse_project_config(text, path)


SETTINGS_FILE_KEYS: dict[str, str] = {
    "repoach.integration_branch": "REPOACH_INTEGRATION_BRANCH",
    "repoach.release_branch": "REPOACH_RELEASE_BRANCH",
    "state.db_path": "db_path",
    "state.logs_dir": "logs_dir",
}
"""``repoach.toml`` keys :class:`~repoach.core.config.Settings` reads, by init key."""


def settings_file_values(root: Path) -> dict[str, str]:
    """Return the :class:`~repoach.core.config.Settings` init values the file sets.

    Only keys the file sets explicitly are returned; paths are anchored
    to ``root``.  The caller decides precedence against the environment.

    Args:
        root: The project root.

    Returns:
        ``{settings init key: value}``.

    Raises:
        ConfigError: The file is invalid.
    """
    config = load_project_config(root)
    values: dict[str, str] = {}
    for dotted, init_key in SETTINGS_FILE_KEYS.items():
        if not config.is_set(dotted):
            continue
        section, _, key = dotted.partition(".")
        raw = str(getattr(getattr(config, section), key))
        if not raw:
            continue
        values[init_key] = str((root / raw).resolve()) if section == "state" else raw
    return values


def specs_dir(root: Path) -> Path:
    """Return the configured spec directory under ``root``.

    Args:
        root: The repository root.

    Returns:
        ``root / [repoach].specs_dir`` (``docs/specs`` by default).

    Raises:
        ConfigError: ``root/repoach.toml`` is invalid.
    """
    return root / load_project_config(root).repoach.specs_dir


def plans_dir_relative(root: Path | None) -> str:
    """Return the configured plan directory, relative to ``root``.

    Args:
        root: The repository root; None means the built-in default.

    Returns:
        ``[repoach].plans_dir`` (``docs/plans`` by default).

    Raises:
        ConfigError: ``root/repoach.toml`` is invalid.
    """
    if root is None:
        return DEFAULT_PLANS_DIR
    return load_project_config(root).repoach.plans_dir
