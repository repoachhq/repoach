"""The ``[[review.generated]]`` declarations, read at the base ref (G1).

Parsing and the per-entry shape rules are :mod:`repoach.project.config`'s;
this module adds the rules across entries and those needing the language
table or git, applied where the review loads the declarations (so a plain
``repoach.toml`` parse stays a shape check): no two entries declare one
path, no ``path`` hides an entry's check script, sources or producer
inputs, no ``path`` glob matches a file of a declared language, the
declarations are read from the base ref only, and two entries may not
match one changed path (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..gates.languages import Language, spec_for
from ..project.config import (
    CONFIG_FILE_NAME,
    ConfigError,
    GeneratedSection,
    ProjectConfig,
    array_table_line,
    parse_project_config,
)
from ..project.globs import GLOB_SPECIALS, glob_code_extension, glob_matches
from .generated_git import blob_at
from .generated_types import GeneratedEntry


def refuse_duplicate_generated(path: Path, text: str, entries: Sequence[GeneratedSection]) -> None:
    """Refuse two ``[[review.generated]]`` entries declaring the same ``path``.

    Args:
        path: The file, for the message.
        text: The file content, for the line lookup.
        entries: The parsed entries.

    Raises:
        ConfigError: Two entries declare the same ``path``; the second is named.
    """
    seen: dict[str, int] = {}
    for index, entry in enumerate(entries):
        if entry.path in seen:
            raise ConfigError(
                path,
                f"{entry.path!r} is already declared by review.generated[{seen[entry.path]}]",
                key=f"review.generated[{index}].path",
                line=array_table_line(text, "review.generated", index, "path"),
            )
        seen[entry.path] = index


def _hides(pattern: str, target: str) -> bool:
    """Whether the ``path`` glob ``pattern`` can match ``target``, a path or a glob.

    A literal ``target`` is matched directly; a glob ``target`` is matched
    the other way round when ``pattern`` is literal (``data/*`` as a
    source, ``data/x.json`` as the artefact); two globs overlap when the
    literal head of one starts the other's (``docs/**`` and
    ``docs/user_guide/**``), a conservative answer.
    """
    if not any(char in target for char in GLOB_SPECIALS):
        return glob_matches(pattern, target)
    if not any(char in pattern for char in GLOB_SPECIALS):
        return glob_matches(target, pattern)
    head = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    other = re.split(r"[*?\[]", target, maxsplit=1)[0]
    return head.startswith(other) or other.startswith(head)


def refuse_hidden_provers(path: Path, text: str, entries: Sequence[GeneratedSection]) -> None:
    """Refuse a ``path`` glob that would summarise away a check script or a source.

    The check script and the sources are what the reviewers read to trust
    the derivation; an entry whose ``path`` matches its own, or another
    entry's, ``check[0]``, ``sources`` or ``producer_inputs`` would hide
    the prover behind its own summary. Two globs are compared by their
    literal heads (:func:`_hides`), which refuses some declarations that
    could not overlap (``data/x/*.npy`` beside a ``data/*.csv``
    source): fail closed, on purpose.

    Args:
        path: The file, for the message.
        text: The file content, for the line lookup.
        entries: The parsed entries.

    Raises:
        ConfigError: A ``path`` glob matches a check script, a source or a
            producer input of any entry.
    """
    for index, entry in enumerate(entries):
        for other in entries:
            for target in (other.check[0], *other.sources, *other.producer_inputs):
                if _hides(entry.path, target):
                    raise ConfigError(
                        path,
                        f"{entry.path!r} can match {target}, a check script or source, "
                        "which the reviewers must read",
                        key=f"review.generated[{index}].path",
                        line=array_table_line(text, "review.generated", index, "path"),
                    )


def refuse_code_globs(
    path: Path, text: str, entries: Sequence[GeneratedSection], extensions: tuple[str, ...]
) -> None:
    """Refuse a ``[[review.generated]]`` ``path`` glob that could match a code file.

    The plain file parse cannot import the language table (it would cycle
    through the settings), so the review's loader calls this with the
    declared languages' extensions.

    Args:
        path: The file, for the message.
        text: The file content, for the line lookup.
        entries: The parsed entries.
        extensions: The file extensions of the declared languages.

    Raises:
        ConfigError: A ``path`` glob can match a file with one of ``extensions``.
    """
    for index, entry in enumerate(entries):
        suffix = glob_code_extension(entry.path, extensions)
        if suffix is not None:
            raise ConfigError(
                path,
                f"{entry.path!r} can match {suffix} files, code of a declared language; "
                "code is reviewed, never declared generated",
                key=f"review.generated[{index}].path",
                line=array_table_line(text, "review.generated", index, "path"),
            )


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
    entries = config.review.generated
    refuse_duplicate_generated(path, base_toml_text, entries)
    refuse_hidden_provers(path, base_toml_text, entries)
    refuse_code_globs(path, base_toml_text, entries, code_extensions(declared))
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


def present_at_base(
    root: Path, base: str, touched: Mapping[str, GeneratedEntry]
) -> dict[str, GeneratedEntry]:
    """Keep the touched artefacts that already exist at the base; a new file is reviewed in full.

    A wildcard ``path`` would otherwise let a pull request add an
    unrelated file under it and have it summarised away, proved only by a
    check that may never read it. The first version of an artefact is
    read like any file (its oversized finding included); later changes
    are summarised and checked.

    Args:
        root: Any checkout of the repository.
        base: The merge base the diff was read from.
        touched: The declared artefacts the diff changes.

    Returns:
        The subset whose path has a blob at ``base``, in the same order.
    """
    return {path: entry for path, entry in touched.items() if blob_at(root, base, path) is not None}


__all__ = [
    "code_extensions",
    "declared_at_base",
    "load_generated_entries",
    "present_at_base",
    "refuse_code_globs",
    "refuse_duplicate_generated",
    "refuse_hidden_provers",
    "touched_artefacts",
]
