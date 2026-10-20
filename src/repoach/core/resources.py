"""Package resources shipped with repoach (SP-UNIVERSAL-INSTALL).

The versioned reviewer/coder/developer prompts live in ``prompts/review``
at the repository root and are force-included in the wheel as
``repoach/prompts/review`` (``pyproject.toml``,
``[tool.hatch.build.targets.wheel.force-include]``).  :func:`prompts_dir`
is the one place that knows both layouts.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

PROMPTS_SUBDIR = Path("prompts") / "review"
"""Location of the prompt files, relative to the package or the checkout root."""


def prompts_dir() -> Path:
    """Return the directory holding the versioned ``prompts/review/*.md`` files.

    An installed wheel carries them inside the package; a source
    checkout (editable install) keeps them at the repository root,
    next to ``src/``.

    Returns:
        The absolute prompts directory.

    Raises:
        FileNotFoundError: Neither layout has the directory -- the
            installation is broken, and no prompt can be loaded.
    """
    packaged = Path(str(files("repoach"))) / PROMPTS_SUBDIR
    if packaged.is_dir():
        return packaged
    checkout = Path(__file__).resolve().parents[3] / PROMPTS_SUBDIR
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError(f"repoach prompts not found at {packaged} nor {checkout}")


SPEC_TEMPLATE_PACKAGED = Path("project") / "spec_template.md"
"""Location of the spec template inside an installed wheel."""

SPEC_TEMPLATE_CHECKOUT = Path("docs") / "specs" / "_TEMPLATE.md"
"""Location of the spec template in a source checkout."""


def spec_template_path() -> Path:
    """Return the spec template ``repoach init`` copies into a host repository.

    The wheel force-includes ``docs/specs/_TEMPLATE.md`` as
    ``repoach/project/spec_template.md``; a source checkout reads it in
    place, so there is exactly one template file in the repository.

    Returns:
        The absolute template path.

    Raises:
        FileNotFoundError: Neither layout has the file.
    """
    packaged = Path(str(files("repoach"))) / SPEC_TEMPLATE_PACKAGED
    if packaged.is_file():
        return packaged
    checkout = Path(__file__).resolve().parents[3] / SPEC_TEMPLATE_CHECKOUT
    if checkout.is_file():
        return checkout
    raise FileNotFoundError(f"repoach spec template not found at {packaged} nor {checkout}")
