"""The ``[[review.generated]]`` declarations, read at the base ref (G1).

Parsing and shape rules are :mod:`repoach.project.config`'s; this module
adds what needs the language table and git: a ``path`` glob may never
match a file of a declared language, the declarations are read from the
base ref only, and two entries may not match one changed path
(SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ..gates.languages import Language, spec_for
from ..project.config import (
    CONFIG_FILE_NAME,
    ConfigError,
    ProjectConfig,
    parse_project_config,
    refuse_code_globs,
)
from .generated_git import blob_at
from .generated_types import GeneratedEntry


def _entries(config: ProjectConfig) -> tuple[GeneratedEntry, ...]:
    """Turn the validated ``[[review.generated]]`` sections into entries."""
    return tuple(
        GeneratedEntry(
            path=section.path,
            check=tuple(section.check),
            sources=tuple(section.sources),
            producer_inputs=tuple(section.producer_inputs),
            check_timeout_s=section.check_timeout_s,
        )
        for section in config.review.generated
    )


def load_generated_entries(
    base_toml_text: str, path: Path, languages: Sequence[str]
) -> tuple[GeneratedEntry, ...]:
    """Parse the ``[[review.generated]]`` entries of a ``repoach.toml`` read at the base ref.

    Args:
        base_toml_text: The file's content at the base ref.
        path: Where it came from, for messages.
        languages: Languages whose files a ``path`` glob may never match,
            besides the file's own ``[project].languages``.

    Returns:
        The entries, in declaration order.

    Raises:
        ConfigError: A malformed entry, a glob matching code, or two
            entries declaring the same path; the message names the key
            and the line.
    """
    config = parse_project_config(base_toml_text, path)
    declared = (*config.project.languages, *languages)
    refuse_code_globs(path, base_toml_text, config.review.generated, code_extensions(declared))
    return _entries(config)


def code_extensions(languages: Sequence[str]) -> tuple[str, ...]:
    """Return the file extensions of every named language (``[project].languages``).

    Args:
        languages: Language names; unknown names contribute nothing.

    Returns:
        The extensions, with their dot, in order, each once.
    """
    known = {language.value for language in Language}
    extensions: list[str] = []
    for name in languages:
        spec = spec_for(Language(name)) if name in known else None
        if spec is not None:
            extensions.extend(ext for ext in spec.extensions if ext not in extensions)
    return tuple(extensions)


def declared_at_base(root: Path, base_ref: str | None) -> tuple[GeneratedEntry, ...]:
    """Read the generated-artefact declarations of ``repoach.toml`` at the base ref (G1).

    The head never declares: an entry a PR adds applies from the next PR on.

    Args:
        root: Any checkout of the repository (refs are shared).
        base_ref: The base ref; ``None`` declares nothing.

    Returns:
        The entries, empty when the base has no file or no entry.

    Raises:
        ConfigError: The base file is invalid.
    """
    if not base_ref:
        return ()
    text = blob_at(root, base_ref, CONFIG_FILE_NAME)
    if text is None:
        return ()
    decoded = text.decode("utf-8", errors="replace")
    return load_generated_entries(decoded, Path(f"{base_ref}:{CONFIG_FILE_NAME}"), ())


def touched_artefacts(
    entries: Sequence[GeneratedEntry], diff_files: Sequence[str]
) -> dict[str, GeneratedEntry]:
    """Return every changed path a declared ``path`` matches, in declaration order.

    Args:
        entries: The declarations read at the base.
        diff_files: The paths the diff changes.

    Returns:
        ``{changed path: its entry}``; empty when the diff touches none.

    Raises:
        ConfigError: Two entries match the same changed path.
    """
    touched: dict[str, GeneratedEntry] = {}
    for entry in entries:
        for path in diff_files:
            if not entry.matches(path):
                continue
            other = touched.get(path)
            if other is not None and other is not entry:
                raise ConfigError(
                    Path(CONFIG_FILE_NAME),
                    f"{path} is matched by two entries ({other.path!r} and {entry.path!r})",
                    key="review.generated",
                )
            touched[path] = entry
    return touched


__all__ = ["code_extensions", "declared_at_base", "load_generated_entries", "touched_artefacts"]
