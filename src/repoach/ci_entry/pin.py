"""Pin the repoach a generated CI job installs to the repoach that generated it.

A CI job must not install a moving branch: the review it runs would
change under the project without a commit of its own. ``init --ci``
therefore pins, by default, the version running ``init`` itself, in
this order:

1. installed from git (``pip install git+…@<sha>``): the commit pip
   recorded in ``direct_url.json`` (PEP 610);
2. a repoach git checkout (an editable install, or ``PYTHONPATH=src``;
   the package must sit at ``<toplevel>/src/repoach``, so a wheel inside
   a project's ``.venv`` never pins the project's own ``HEAD``): the
   checkout's ``HEAD`` SHA, with a note when the tree is dirty, since
   uncommitted changes are not in the pin;
3. otherwise (a wheel from PyPI): ``repoach==<version>``.

``--ref`` and ``--pypi-version`` override the default.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib import metadata
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .. import __version__
from ..core.logging import get_logger

_log = get_logger(__name__)

DISTRIBUTION = "repoach"
_PACKAGE_DIR = Path(__file__).resolve().parents[1]


class InstallTarget(BaseModel):
    """What the job installs: a git ref of repoach or a PyPI version.

    Attributes:
        ref: Git ref (tag or SHA) of the repoach repository.
        pypi_version: Exact PyPI version; wins over ``ref``.
        provenance: Where the pin came from, printed by ``init --ci``.
    """

    model_config = ConfigDict(frozen=True)

    ref: str | None = None
    pypi_version: str | None = None
    provenance: str = "flag"

    def requirement(self, git_url: str) -> str:
        """Return the pip requirement string.

        Raises:
            ValueError: Neither a ref nor a PyPI version is set.
        """
        if self.pypi_version:
            return f"repoach=={self.pypi_version}"
        if self.ref:
            return f"repoach @ git+{git_url}@{self.ref}"
        raise ValueError("an install target needs a ref or a PyPI version")

    def describe(self) -> str:
        """Return ``repoach==X`` or ``repoach @ <ref>`` with its provenance."""
        pinned = f"repoach=={self.pypi_version}" if self.pypi_version else f"repoach @ {self.ref}"
        return f"{pinned} ({self.provenance})"


def _direct_url() -> dict[str, object] | None:
    """Return the installed distribution's PEP 610 record, None when absent."""
    try:
        raw = metadata.distribution(DISTRIBUTION).read_text("direct_url.json")
    except metadata.PackageNotFoundError as exc:
        _log.debug("ci_pin_no_distribution", error=str(exc))
        return None
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _log.warning("ci_pin_direct_url_unreadable", error=str(exc))
        return None
    return payload if isinstance(payload, dict) else None


def _git(source_dir: Path, *args: str) -> str | None:
    """Run ``git -C source_dir args``; None when git is absent or fails."""
    git = shutil.which("git")
    if git is None:
        return None
    completed = subprocess.run(
        [git, "-C", str(source_dir), *args], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def resolve_pin(
    *,
    direct_url: dict[str, object] | None,
    source_dir: Path,
    version: str,
) -> InstallTarget:
    """Decide the default pin from the running installation.

    Args:
        direct_url: The PEP 610 record of the installed distribution.
        source_dir: The directory holding the ``repoach`` package.
        version: The running repoach version.

    Returns:
        A commit SHA pin, or a PyPI version pin.
    """
    vcs = direct_url.get("vcs_info") if direct_url is not None else None
    if isinstance(vcs, dict):
        commit = vcs.get("commit_id")
        if isinstance(commit, str) and commit:
            return InstallTarget(ref=commit, provenance="the git commit pip installed")
    toplevel = _git(source_dir, "rev-parse", "--show-toplevel")
    is_checkout = toplevel is not None and (
        (Path(toplevel) / "src" / "repoach").resolve() == source_dir.resolve()
    )
    head = _git(source_dir, "rev-parse", "HEAD") if is_checkout else None
    if head:
        dirty = _git(source_dir, "status", "--porcelain", "--untracked-files=no")
        note = "; uncommitted changes are not in it" if dirty else ""
        return InstallTarget(
            ref=head,
            provenance=f"HEAD of the checkout running init, push it before the job runs{note}",
        )
    return InstallTarget(pypi_version=version, provenance="the running version, from PyPI")


def default_pin() -> InstallTarget:
    """Return the pin for the repoach running this process."""
    return resolve_pin(direct_url=_direct_url(), source_dir=_PACKAGE_DIR, version=__version__)
