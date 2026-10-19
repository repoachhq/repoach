"""What ``repoach init`` writes into a host repository, and nothing more (G2, G3).

Three paths at most, all under the project root:

* ``repoach.toml`` — rendered from :func:`~repoach.project.measure.measure_defaults`;
  kept as-is when present unless ``--force``;
* ``<specs_dir>/_TEMPLATE.md`` — the shipped spec template; never
  overwritten, even with ``--force``;
* ``.gitignore`` — gains a ``.repoach.env`` line, only when the file
  already exists and does not list it.

Nothing else is touched: no ``git config``, nothing under ``.git/``,
no hook, CI or agent directory, no ``.env*`` file.  :func:`plan_init`
computes every action without writing, so ``--dry-run`` and the real
run print the same plan.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..core.paths import PROJECT_ENV_FILE_NAME
from ..core.resources import spec_template_path
from .config import CONFIG_FILE_NAME, DEFAULT_SPECS_DIR, load_project_config
from .measure import Measurement, measure_defaults
from .toml_write import render_project_toml

SPEC_TEMPLATE_NAME = "_TEMPLATE.md"
"""File name of the spec template inside the spec directory."""

GITIGNORE_NAME = ".gitignore"

Verb = Literal["create", "rewrite", "append", "unchanged"]


class InitRefusedError(RuntimeError):
    """``init --force`` would discard uncommitted ``repoach.toml`` changes."""


@dataclass(frozen=True)
class InitAction:
    """One file ``init`` creates, rewrites, appends to or leaves alone.

    Attributes:
        path: Repository-relative path.
        verb: What happens to it.
        content: Full new content (``create`` / ``rewrite``) or the text
            appended (``append``); empty for ``unchanged``.
    """

    path: str
    verb: Verb
    content: str = ""

    def describe(self, *, dry_run: bool) -> str:
        """Return the one output line for this action.

        Args:
            dry_run: Whether nothing is written.

        Returns:
            ``init: created repoach.toml``, ``init: repoach.toml unchanged``,
            ``init: would create …`` and so on.
        """
        if self.verb == "unchanged":
            return f"init: {self.path} unchanged"
        past = {"create": "created", "rewrite": "rewrote", "append": "updated"}[self.verb]
        detail = f" (+ {PROJECT_ENV_FILE_NAME})" if self.verb == "append" else ""
        if dry_run:
            future = {"create": "create", "rewrite": "rewrite", "append": "update"}[self.verb]
            return f"init: would {future} {self.path}{detail}"
        return f"init: {past} {self.path}{detail}"


@dataclass(frozen=True)
class InitPlan:
    """Every action of one ``init`` run, plus the measurement notes."""

    actions: list[InitAction]
    notes: list[str]


def _has_uncommitted_changes(root: Path, relpath: str) -> bool:
    """Return whether ``git status`` reports ``relpath`` as modified or untracked."""
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--", relpath],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(completed.stdout.strip())


def _gitignore_action(root: Path) -> InitAction | None:
    """Return the ``.gitignore`` action, or None when the repository has none."""
    path = root / GITIGNORE_NAME
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    listed = {line.strip() for line in text.splitlines()}
    if PROJECT_ENV_FILE_NAME in listed or f"/{PROJECT_ENV_FILE_NAME}" in listed:
        return InitAction(GITIGNORE_NAME, "unchanged")
    prefix = "" if not text or text.endswith("\n") else "\n"
    return InitAction(GITIGNORE_NAME, "append", f"{prefix}{PROJECT_ENV_FILE_NAME}\n")


def plan_init(
    root: Path, *, force: bool = False, yes: bool = False, probe: bool = True
) -> InitPlan:
    """Compute what ``init`` would do in ``root``, writing nothing.

    Args:
        root: The project root (git toplevel).
        force: Re-measure and rewrite an existing ``repoach.toml``.
        yes: Confirm ``force`` over uncommitted ``repoach.toml`` changes.
        probe: Whether the forge API may be asked.

    Returns:
        The plan.

    Raises:
        InitRefusedError: ``force`` without ``yes`` while ``repoach.toml``
            has uncommitted changes.
        ConfigError: An existing ``repoach.toml`` kept as-is is invalid.
    """
    config_file = root / CONFIG_FILE_NAME
    actions: list[InitAction] = []
    notes: list[str] = []
    specs_dir = DEFAULT_SPECS_DIR
    if config_file.is_file() and not force:
        actions.append(InitAction(CONFIG_FILE_NAME, "unchanged"))
        specs_dir = load_project_config(root).repoach.specs_dir
    else:
        if config_file.is_file() and not yes and _has_uncommitted_changes(root, CONFIG_FILE_NAME):
            raise InitRefusedError(
                f"{CONFIG_FILE_NAME} has uncommitted changes; --force would discard them "
                "(add --yes to confirm)"
            )
        measurement: Measurement = measure_defaults(root, probe=probe)
        notes.extend(measurement.notes)
        content = render_project_toml(measurement.values)
        specs_value = measurement.values.get("repoach.specs_dir", DEFAULT_SPECS_DIR)
        specs_dir = specs_value if isinstance(specs_value, str) else DEFAULT_SPECS_DIR
        if not config_file.is_file():
            actions.append(InitAction(CONFIG_FILE_NAME, "create", content))
        elif config_file.read_text(encoding="utf-8") == content:
            actions.append(InitAction(CONFIG_FILE_NAME, "unchanged"))
        else:
            actions.append(InitAction(CONFIG_FILE_NAME, "rewrite", content))

    template_rel = f"{specs_dir.rstrip('/')}/{SPEC_TEMPLATE_NAME}"
    if (root / template_rel).exists():
        actions.append(InitAction(template_rel, "unchanged"))
    else:
        template = spec_template_path().read_text(encoding="utf-8")
        actions.append(InitAction(template_rel, "create", template))

    gitignore = _gitignore_action(root)
    if gitignore is not None:
        actions.append(gitignore)
    return InitPlan(actions=actions, notes=notes)


def apply_init(root: Path, plan: InitPlan) -> None:
    """Write the plan's actions under ``root``.

    Args:
        root: The project root.
        plan: A plan from :func:`plan_init`.
    """
    for action in plan.actions:
        target = root / action.path
        if action.verb in ("create", "rewrite"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action.content, encoding="utf-8")
        elif action.verb == "append":
            with target.open("a", encoding="utf-8") as handle:
                handle.write(action.content)
