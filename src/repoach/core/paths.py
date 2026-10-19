"""Filesystem anchors for a repoach run (SP-UNIVERSAL-INSTALL).

Every module that needs a location asks this one: the project being
worked on is the git toplevel of the current working directory, user
configuration lives under the XDG config directory, and state (the
ledger) lives under the XDG data directory, one subdirectory per
project.  Nothing here resolves from the package's own ``__file__``:
on a wheel or tool install the package sits in ``site-packages``, and
an ancestor ``pyproject.toml`` of the package belongs to whatever
project happens to host the virtualenv, never to repoach.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

APP_DIR_NAME = "repoach"
"""Directory name used under both XDG base directories."""

DB_FILE_NAME = "repoach.db"
"""File name of the SQLite ledger inside a project's data directory."""

USER_ENV_FILE_NAME = "env"
"""Dotenv file read from :func:`user_config_dir` for every project."""

PROJECT_ENV_FILE_NAME = ".repoach.env"
"""Dotenv file read from :func:`project_root`; never the host's ``.env``."""

_SCP_REMOTE_RE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")
"""``user@host:owner/repo.git`` — the scp-like syntax git accepts for SSH."""

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
"""Characters replaced in a slug segment so it is always a plain path part."""


class NotARepositoryError(RuntimeError):
    """A command that needs a git working tree was run outside one.

    Attributes:
        cwd: The directory the lookup started from.
    """

    def __init__(self, cwd: Path, reason: str = "not a git repository") -> None:
        self.cwd = cwd
        super().__init__(f"{reason}: {cwd}")


def _git_toplevel(start: Path) -> Path | None:
    """Return the git toplevel containing ``start``, or None outside a work tree.

    Args:
        start: Directory the lookup starts from.

    Returns:
        The absolute toplevel of the working tree (a linked worktree's
        own root, not its main checkout), or None when ``start`` is not
        inside any git working tree.

    Raises:
        NotARepositoryError: The ``git`` executable is not available, so
            the question cannot be answered at all.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NotARepositoryError(start, "git executable not found") from exc
    toplevel = completed.stdout.strip()
    if completed.returncode != 0 or not toplevel:
        return None
    return Path(toplevel).resolve()


def project_root(start: Path | None = None) -> Path:
    """Return the project repoach works on: the git toplevel of ``start``.

    Outside a git working tree the directory itself is returned, so a
    command that only reads configuration still works there; commands
    that need a repository call :func:`require_project_root` instead.

    Args:
        start: Directory to resolve from; defaults to the current
            working directory.

    Returns:
        The absolute project root.

    Raises:
        NotARepositoryError: ``git`` is not installed.
    """
    origin = (start if start is not None else Path.cwd()).resolve()
    toplevel = _git_toplevel(origin)
    return toplevel if toplevel is not None else origin


def require_project_root(start: Path | None = None) -> Path:
    """Return the git toplevel of ``start`` or refuse.

    Args:
        start: Directory to resolve from; defaults to the current
            working directory.

    Returns:
        The absolute git toplevel.

    Raises:
        NotARepositoryError: ``start`` is not inside a git working tree,
            or ``git`` is not installed.
    """
    origin = (start if start is not None else Path.cwd()).resolve()
    toplevel = _git_toplevel(origin)
    if toplevel is None:
        raise NotARepositoryError(origin)
    return toplevel


def _xdg_base(variable: str, fallback: Path) -> Path:
    """Return an XDG base directory, honouring only absolute overrides.

    The XDG Base Directory specification says a relative value must be
    ignored, so a relative ``XDG_*`` falls back exactly like an unset one.

    Args:
        variable: Name of the XDG environment variable.
        fallback: Default used when the variable is unset, empty or relative.

    Returns:
        The base directory, not yet suffixed with the application name.
    """
    raw = os.environ.get(variable, "")
    if raw and Path(raw).is_absolute():
        return Path(raw)
    return fallback


def user_config_dir() -> Path:
    """Return ``$XDG_CONFIG_HOME/repoach`` (default ``~/.config/repoach``).

    Returns:
        The per-user configuration directory; it may not exist yet.
    """
    return _xdg_base("XDG_CONFIG_HOME", Path.home() / ".config") / APP_DIR_NAME


def user_data_dir() -> Path:
    """Return ``$XDG_DATA_HOME/repoach`` (default ``~/.local/share/repoach``).

    Returns:
        The per-user data directory; it may not exist yet.
    """
    return _xdg_base("XDG_DATA_HOME", Path.home() / ".local" / "share") / APP_DIR_NAME


def env_files(root: Path) -> tuple[Path, Path]:
    """Return the dotenv files repoach reads, lowest precedence first.

    The process environment wins over both.  The host repository's own
    ``.env`` and any file under the installed package are never part of
    this list.

    Args:
        root: The project root (see :func:`project_root`).

    Returns:
        ``(user_config_dir()/env, root/.repoach.env)``.
    """
    return (user_config_dir() / USER_ENV_FILE_NAME, root / PROJECT_ENV_FILE_NAME)


def _origin_url(root: Path) -> str | None:
    """Return the URL of the ``origin`` remote of ``root``, if any.

    Args:
        root: A directory inside a git working tree (or not).

    Returns:
        The configured URL, or None when there is no ``origin`` remote
        or ``root`` is not a repository.

    Raises:
        NotARepositoryError: The ``git`` executable is not available.
    """
    try:
        completed = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NotARepositoryError(root, "git executable not found") from exc
    url = completed.stdout.strip()
    return url if completed.returncode == 0 and url else None


def _slug_from_remote(url: str) -> str | None:
    """Turn a git remote URL into ``host/owner/.../repo``.

    Credentials, ports and the ``.git`` suffix are dropped, so a token
    embedded in an HTTPS remote never reaches a filesystem path.

    Args:
        url: A remote URL in URL form or scp-like SSH form.

    Returns:
        The slug, or None when the URL carries no host or no path.
    """
    if "://" in url:
        parts = urlsplit(url)
        host = parts.hostname or ""
        path = parts.path
    else:
        match = _SCP_REMOTE_RE.match(url)
        if match is None:
            return None
        host = match.group("host")
        path = match.group("path")
    path = path.strip("/")
    path = path.removesuffix(".git")
    segments = [host.lower(), *path.split("/")]
    cleaned = [_SAFE_SEGMENT_RE.sub("-", segment).strip(".-") for segment in segments]
    cleaned = [segment for segment in cleaned if segment]
    if len(cleaned) < 2:
        return None
    return "/".join(cleaned)


def project_slug(root: Path) -> str:
    """Return the per-project directory name under :func:`user_data_dir`.

    Derived from the ``origin`` remote (``github.com/owner/repo``) so
    every clone and worktree of one repository shares a ledger;
    without a usable remote, from a hash of the absolute path.

    Args:
        root: The project root.

    Returns:
        A relative, ``/``-separated slug with no ``..`` segment.

    Raises:
        NotARepositoryError: The ``git`` executable is not available.
    """
    url = _origin_url(root)
    slug = _slug_from_remote(url) if url is not None else None
    if slug is not None:
        return slug
    absolute = root.resolve()
    digest = hashlib.sha256(str(absolute).encode("utf-8")).hexdigest()[:16]
    name = _SAFE_SEGMENT_RE.sub("-", absolute.name).strip(".-") or "root"
    return f"local/{name}-{digest}"


def default_db_path(root: Path) -> Path:
    """Return the default ledger path for the project at ``root``.

    Args:
        root: The project root.

    Returns:
        ``user_data_dir()/<project_slug(root)>/repoach.db``.

    Raises:
        NotARepositoryError: The ``git`` executable is not available.
    """
    return user_data_dir() / project_slug(root) / DB_FILE_NAME
