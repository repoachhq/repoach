"""``repoach ci review``, plus the CI pieces ``doctor --ci`` and ``init --ci`` print.

The command is settings-free (it builds no :class:`~repoach.core.config.Settings`
in this process): an invalid ``repoach.toml`` or a missing gateway is an
outcome in the JSON, not a bootstrap failure, so an advisory job stays
green. The review itself runs in a child ``repoach review pr`` whose
environment :mod:`repoach.ci_entry.advisory` builds.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

import typer

from ..core.logging import get_logger
from ..core.paths import NotARepositoryError, require_project_root
from ..forge import select_forge
from ..project.config import ConfigError, ForgeName
from ..project.resolve import effective_bot_username, resolve_project_config
from .advisory import (
    DEFAULT_REVIEW_TIMEOUT_S,
    EXIT_CODE_TABLE,
    CiReviewPlan,
    CiReviewReport,
    Outcome,
    apply_forge_env,
    finish,
    plan_ci_review,
    provider_mode,
    review_mode,
    run_ci_review,
    subprocess_review_runner,
)
from .context import (
    InvalidPullRequestError,
    NoPullRequestError,
    detect_ci_forge,
    env_value,
    resolve_pr_number,
)
from .gateway import loopback_gateway
from .variables import CI_VARIABLES

_log = get_logger(__name__)

ci_app = typer.Typer(
    help="Run repoach from a CI job (GitLab CI, GitHub Actions).", no_args_is_help=True
)

_EXIT_HELP = "\n\n".join(f"{code}: {meaning}" for code, meaning in EXIT_CODE_TABLE)


def emit(report: CiReviewReport) -> None:
    """Print the report: one JSON document on stdout, human lines on stderr.

    Raises:
        typer.Exit: With the report's exit code.
    """
    where = f" #{report.pr_number}" if report.pr_number is not None else ""
    forge = report.forge or "no forge"
    typer.echo(
        f"repoach ci review: {report.outcome.value}{where} on {forge} "
        f"({report.mode}, provider {report.provider_mode}): {report.reason}",
        err=True,
    )
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
    raise typer.Exit(code=report.exit_code)


def _forge_flag(value: str | None) -> ForgeName | None:
    """Validate ``--forge``.

    Raises:
        ValueError: Neither ``github`` nor ``gitlab``.
    """
    if value is None or not value.strip():
        return None
    name = value.strip().lower()
    if name == "github" or name == "gitlab":
        return name
    raise ValueError(f"unknown forge {value!r}; use github or gitlab")


@ci_app.command("review")
def ci_review(
    pr: int | None = typer.Option(
        None, "--pr", help="PR / MR number (default: read from the CI environment)."
    ),
    forge_name: str | None = typer.Option(
        None, "--forge", help="github or gitlab (default: the CI running this job)."
    ),
    post: bool = typer.Option(
        False, "--post", help="Post the review (default: REPOACH_ADVISORY_POST=1)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Never post, whatever REPOACH_ADVISORY_POST says."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit non-zero on a skip, an error or REQUEST_CHANGES."
    ),
    timeout_min: float = typer.Option(
        DEFAULT_REVIEW_TIMEOUT_S / 60,
        "--timeout",
        help="Minutes before the review child is killed with its process group.",
    ),
) -> None:
    """Review the PR / MR of this CI job; advisory unless --strict.

    Prints one JSON document on stdout (``outcome``, ``reason``, ``forge``,
    ``pr_number``, ``pr_source``, ``mode``, ``provider_mode``,
    ``final_verdict``, ``posted_comments``, ``posted_reviews``, ``review``)
    and one human line on stderr. A missing input (no PR, no forge token,
    no provider) is a named skip.

    Exit codes:

    {exit_codes}
    """
    try:
        report = review_from_env(
            dict(os.environ),
            pr=pr,
            forge_name=forge_name,
            post=post,
            dry_run=dry_run,
            strict=strict,
            timeout_s=timeout_min * 60,
        )
    except Exception as exc:
        _log.error("ci_review_internal_error", error=repr(exc))
        report = finish(Outcome.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}", strict=strict)
    emit(report)


def review_from_env(
    env: dict[str, str],
    *,
    pr: int | None,
    forge_name: str | None,
    post: bool,
    dry_run: bool,
    strict: bool,
    timeout_s: float = DEFAULT_REVIEW_TIMEOUT_S,
) -> CiReviewReport:
    """Run ``ci review`` from the checkout in the working directory; return its report.

    Args:
        env: The job environment.
        pr: ``--pr``.
        forge_name: ``--forge``.
        post: ``--post``.
        dry_run: ``--dry-run``.
        strict: ``--strict``.
        timeout_s: Deadline of the review child.

    Returns:
        The report :func:`emit` prints.
    """
    try:
        forge_override = _forge_flag(forge_name)
        root = require_project_root()
        resolved = resolve_project_config(root)
    except (ValueError, NotARepositoryError, ConfigError) as exc:
        return finish(Outcome.CONFIG_ERROR, str(exc), strict=strict)
    planned = plan_ci_review(
        env,
        pr_override=pr,
        forge_override=forge_override,
        project_forge=lambda: (resolved.forge, resolved.source("repoach.forge")),
        post=post,
        dry_run=dry_run,
        strict=strict,
    )
    if isinstance(planned, CiReviewReport):
        return planned
    apply_forge_env(planned, env)

    def read_state(p: CiReviewPlan) -> str:
        return select_forge(resolved, override=p.forge, cwd=root).pr_state(p.pr_number)

    return run_ci_review(
        planned,
        env,
        pr_state=read_state,
        review_runner=subprocess_review_runner(root, timeout_s=timeout_s),
        gateway_starter=loopback_gateway,
        bot_username=effective_bot_username(resolved, planned.forge),
    )


ci_review.__doc__ = (ci_review.__doc__ or "").replace("{exit_codes}", _EXIT_HELP)


def ci_doctor_lines(env: Mapping[str, str]) -> list[str]:
    """Return the ``doctor --ci`` lines: what ``ci review`` would read, names only.

    Args:
        env: The environment.

    Returns:
        One ``name=present|absent  (group: purpose)`` line per variable,
        then the derived ``ci``, ``pr_source``, ``mode`` and
        ``provider_mode`` lines. No value is ever printed.
    """
    lines: list[str] = []
    forge, forge_source = detect_ci_forge(env)
    lines.append(f"ci={forge or 'none'} ({forge_source})")
    for variable in CI_VARIABLES:
        state = "present" if env_value(env, variable.name) is not None else "absent"
        lines.append(f"{variable.name}={state}  ({variable.group}: {variable.purpose})")
    try:
        _, source = resolve_pr_number(None, env)
        lines.append(f"pr_source={source}")
    except InvalidPullRequestError as exc:
        lines.append(f"pr_source=invalid ({exc.source} is not a PR number)")
    except NoPullRequestError as exc:
        lines.append(f"pr_source=none ({'; '.join(exc.notes) or 'no PR variable set'})")
    lines.append(f"mode={review_mode(env, post=False, dry_run=False)}")
    chosen, missing = provider_mode(env)
    lines.append(f"provider_mode={chosen}" + (f" ({missing})" if missing else ""))
    return lines
