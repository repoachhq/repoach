"""``repoach ci review``: an advisory review from any CI, with one semantics.

Absorbs Yoob's ``ci/review-advisory.sh`` (2026-10-18) into repoach so no
project carries a shell driver:

* a missing input is a named skip, never a red job: no pull request
  (not a merge-request pipeline), no forge token, no provider;
* nothing is posted unless ``REPOACH_ADVISORY_POST`` is exactly ``1``
  (or ``--post``); every other value runs the review with ``--dry-run``;
* two provider modes: **gateway** (``REPOACH_LLM_PROXY_BASE_URL`` +
  ``REPOACH_ANTHROPIC_AUTH_TOKEN``, a gateway already running) and
  **direct** (``REPOACH_ANTHROPIC_API_KEY``: a loopback gateway is
  started for the job, :mod:`repoach.ci_entry.gateway`);
* a team verdict of ``REQUEST_CHANGES`` is told apart from an outage by
  reading the review's JSON, not its exit code (``review pr`` exits 2
  for both);
* before reviewing, the pull request's state is read: a PR merged or
  closed while the job queued ends ``already_merged`` / ``closed``.

Exit codes (:func:`exit_code_for`): advisory mode (the default) always
exits 0, the outcome is in the JSON. ``--strict`` exits 0 reviewed
(APPROVE / COMMENT) or already merged / closed, 1 internal error,
2 configuration (a missing input, an invalid flag or ``repoach.toml``,
a forge or provider unavailable), 3 reviewer error (no model reached),
4 changes requested.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from ..core.logging import get_logger
from ..forge import ForgeError
from ..project.config import ForgeName
from .context import (
    InvalidPullRequestError,
    NoPullRequestError,
    detect_ci_forge,
    env_value,
    first_set,
    resolve_pr_number,
)
from .gateway import GatewayHandle, GatewayStartError
from .variables import (
    DIRECT_KEY_VARS,
    GATEWAY_TOKEN_VAR,
    GATEWAY_URL_VAR,
    GITHUB_TOKEN_VARS,
    GITLAB_TOKEN_VARS,
    POST_VAR,
)

_log = get_logger(__name__)

ReviewMode = Literal["post", "dry-run"]
ProviderMode = Literal["gateway", "direct", "none"]

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_CONFIG = 2
EXIT_REVIEWER_ERROR = 3
EXIT_CHANGES_REQUESTED = 4

EXIT_CODE_TABLE: tuple[tuple[int, str], ...] = (
    (EXIT_OK, "advisory mode: always; --strict: reviewed (APPROVE/COMMENT), merged or closed"),
    (EXIT_INTERNAL, "--strict: internal error (unexpected review exit code)"),
    (EXIT_CONFIG, "--strict: skipped for a missing input, bad flag/config, forge/provider down"),
    (EXIT_REVIEWER_ERROR, "--strict: a reviewer reached no model (verdict ERROR)"),
    (EXIT_CHANGES_REQUESTED, "--strict: the team requested changes"),
)
"""``repoach ci review`` exit codes, printed by ``--help`` and pinned by a test."""

_TOKEN_VARS: dict[ForgeName, tuple[str, ...]] = {
    "gitlab": GITLAB_TOKEN_VARS,
    "github": GITHUB_TOKEN_VARS,
}


class Outcome(StrEnum):
    """What one ``repoach ci review`` run ended in."""

    REVIEWED = "reviewed"
    SKIPPED = "skipped"
    ALREADY_MERGED = "already_merged"
    CLOSED = "closed"
    CONFIG_ERROR = "config_error"
    UNAVAILABLE = "unavailable"
    REVIEWER_ERROR = "reviewer_error"
    INTERNAL_ERROR = "internal_error"


class CiReviewReport(BaseModel):
    """The one JSON document ``repoach ci review`` prints on stdout.

    Attributes:
        outcome: What the run ended in.
        reason: One human line saying why.
        forge: ``github`` / ``gitlab`` / None when unresolved.
        forge_source: Where the forge came from (``flag``, ``GITLAB_CI``…).
        pr_number: The PR / MR number, None when unresolved.
        pr_source: ``flag`` or the variable the number came from.
        mode: ``post`` or ``dry-run``.
        provider_mode: ``gateway``, ``direct`` or ``none``.
        final_verdict: The team verdict when a review ran.
        posted_comments: Comments the review posted (0 on a dry run).
        posted_reviews: Reviews the review posted (0 on a dry run).
        review_exit_code: ``repoach review pr``'s own exit code.
        review: The review's JSON payload, verbatim, when it printed one.
        strict: Whether ``--strict`` was given.
        exit_code: The exit code this run ends with.
    """

    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    reason: str
    forge: ForgeName | None = None
    forge_source: str | None = None
    pr_number: int | None = None
    pr_source: str | None = None
    mode: ReviewMode = "dry-run"
    provider_mode: ProviderMode = "none"
    final_verdict: str | None = None
    posted_comments: int = 0
    posted_reviews: int = 0
    review_exit_code: int | None = None
    review: dict[str, object] | None = None
    strict: bool = False
    exit_code: int = EXIT_OK


class CiReviewPlan(BaseModel):
    """Everything a run needs once every input is present.

    Attributes:
        forge: The forge the PR lives on.
        forge_source: Where the forge came from.
        pr_number: The PR / MR number.
        pr_source: Where the number came from.
        mode: ``post`` or ``dry-run``.
        provider_mode: ``gateway`` or ``direct``.
        forge_token_var: The variable holding the forge token.
        strict: Whether ``--strict`` was given.
    """

    model_config = ConfigDict(frozen=True)

    forge: ForgeName
    forge_source: str
    pr_number: int
    pr_source: str
    mode: ReviewMode
    provider_mode: Literal["gateway", "direct"]
    forge_token_var: str
    strict: bool = False


def exit_code_for(outcome: Outcome, final_verdict: str | None, *, strict: bool) -> int:
    """Map an outcome to the exit code: 0 in advisory mode, the table under ``--strict``."""
    if not strict:
        return EXIT_OK
    if outcome == Outcome.REVIEWED:
        return EXIT_CHANGES_REQUESTED if final_verdict == "REQUEST_CHANGES" else EXIT_OK
    if outcome in (Outcome.ALREADY_MERGED, Outcome.CLOSED):
        return EXIT_OK
    if outcome == Outcome.REVIEWER_ERROR:
        return EXIT_REVIEWER_ERROR
    if outcome == Outcome.INTERNAL_ERROR:
        return EXIT_INTERNAL
    return EXIT_CONFIG


def finish(outcome: Outcome, reason: str, *, strict: bool, **fields: object) -> CiReviewReport:
    """Build the report for ``outcome``, computing its exit code."""
    verdict = fields.get("final_verdict")
    code = exit_code_for(outcome, verdict if isinstance(verdict, str) else None, strict=strict)
    return CiReviewReport.model_validate(
        {"outcome": outcome, "reason": reason, "strict": strict, "exit_code": code, **fields}
    )


def review_mode(env: Mapping[str, str], *, post: bool, dry_run: bool) -> ReviewMode:
    """Return ``post`` only on ``--post`` or ``REPOACH_ADVISORY_POST`` exactly ``1``.

    Raises:
        ValueError: ``--post`` and ``--dry-run`` together.
    """
    if post and dry_run:
        raise ValueError("--post and --dry-run are mutually exclusive")
    if post:
        return "post"
    if dry_run:
        return "dry-run"
    return "post" if env.get(POST_VAR, "").strip() == "1" else "dry-run"


def provider_mode(env: Mapping[str, str]) -> tuple[ProviderMode, str | None]:
    """Choose the provider mode, or name what is missing.

    Gateway mode wins when both its variables are set; otherwise a
    direct key starts a loopback gateway.

    Returns:
        ``(mode, None)`` when usable, ``("none", reason)`` otherwise.
    """
    has_url = env_value(env, GATEWAY_URL_VAR) is not None
    has_token = env_value(env, GATEWAY_TOKEN_VAR) is not None
    if has_url and has_token:
        return "gateway", None
    if first_set(env, DIRECT_KEY_VARS) is not None:
        return "direct", None
    if has_url:
        return (
            "none",
            f"{GATEWAY_TOKEN_VAR} is not set (gateway mode needs it with {GATEWAY_URL_VAR})",
        )
    return "none", (
        f"no provider: set {DIRECT_KEY_VARS[0]} (direct mode) or "
        f"{GATEWAY_URL_VAR} + {GATEWAY_TOKEN_VAR} (gateway mode)"
    )


def plan_ci_review(
    env: Mapping[str, str],
    *,
    pr_override: int | None = None,
    forge_override: ForgeName | None = None,
    project_forge: Callable[[], tuple[ForgeName, str]] | None = None,
    post: bool = False,
    dry_run: bool = False,
    strict: bool = False,
) -> CiReviewPlan | CiReviewReport:
    """Resolve every input, or end with a named skip or configuration error.

    Inputs are checked in the order Yoob's driver checked them: the pull
    request, the forge token, the provider.

    Args:
        env: The environment.
        pr_override: ``--pr``.
        forge_override: ``--forge``.
        project_forge: Fallback outside a CI: the project's configured
            forge and its source (``repoach.toml``, origin).
        post: ``--post``.
        dry_run: ``--dry-run``.
        strict: ``--strict``.

    Returns:
        The plan, or the final report of a run that cannot start.
    """
    try:
        mode = review_mode(env, post=post, dry_run=dry_run)
    except ValueError as exc:
        return finish(Outcome.CONFIG_ERROR, str(exc), strict=strict)
    forge: ForgeName | None
    if forge_override is not None:
        forge, forge_source = forge_override, "flag"
    else:
        forge, forge_source = detect_ci_forge(env)
        if forge is None and project_forge is not None:
            forge, forge_source = project_forge()
    try:
        number, pr_source = resolve_pr_number(pr_override, env)
    except InvalidPullRequestError as exc:
        return finish(
            Outcome.CONFIG_ERROR,
            str(exc),
            strict=strict,
            forge=forge,
            mode=mode,
            forge_source=forge_source,
        )
    except NoPullRequestError as exc:
        return finish(
            Outcome.SKIPPED,
            f"skipped, {exc}",
            strict=strict,
            forge=forge,
            mode=mode,
            forge_source=forge_source,
        )
    common: dict[str, object] = {
        "forge": forge,
        "forge_source": forge_source,
        "pr_number": number,
        "pr_source": pr_source,
        "mode": mode,
    }
    if forge is None:
        return finish(
            Outcome.CONFIG_ERROR,
            "no forge: not GitLab CI nor GitHub Actions; pass --forge github|gitlab",
            strict=strict,
            **common,
        )
    token_vars = _TOKEN_VARS[forge]
    token_var = first_set(env, token_vars)
    if token_var is None:
        return finish(
            Outcome.SKIPPED,
            f"skipped, {' / '.join(token_vars)} is not set",
            strict=strict,
            **common,
        )
    chosen, missing = provider_mode(env)
    if chosen == "none":
        return finish(Outcome.SKIPPED, f"skipped, {missing}", strict=strict, **common)
    return CiReviewPlan(
        forge=forge,
        forge_source=forge_source,
        pr_number=number,
        pr_source=pr_source,
        mode=mode,
        provider_mode=chosen,
        forge_token_var=token_var,
        strict=strict,
    )


def forge_env(plan: CiReviewPlan, env: Mapping[str, str]) -> dict[str, str]:
    """Return the variables the forge CLI needs, derived from the CI ones.

    ``glab`` reads ``GITLAB_TOKEN`` and ``GITLAB_HOST``; ``gh`` reads
    ``GH_TOKEN`` / ``GITHUB_TOKEN`` and ``GH_REPO``. A value already set
    is never replaced.
    """
    derived: dict[str, str] = {"REPOACH_FORGE": plan.forge}
    if plan.forge == "gitlab":
        if env_value(env, "GITLAB_TOKEN") is None:
            derived["GITLAB_TOKEN"] = env[plan.forge_token_var].strip()
        server = env_value(env, "CI_SERVER_URL")
        if env_value(env, "GITLAB_HOST") is None and server is not None:
            derived["GITLAB_HOST"] = urlparse(server).netloc or server
    else:
        repository = env_value(env, "GITHUB_REPOSITORY")
        if env_value(env, "GH_REPO") is None and repository is not None:
            derived["GH_REPO"] = repository
    return derived


def review_env(
    plan: CiReviewPlan, env: Mapping[str, str], gateway: GatewayHandle | None
) -> dict[str, str]:
    """Build the review's environment: the forge token in, the provider key out.

    Args:
        plan: The run plan.
        env: The job environment.
        gateway: The loopback gateway in direct mode, None in gateway mode.

    Returns:
        The environment for ``repoach review pr``.
    """
    child = {k: v for k, v in env.items() if k not in DIRECT_KEY_VARS}
    child.update(forge_env(plan, env))
    if gateway is not None:
        child[GATEWAY_URL_VAR] = gateway.base_url
        child[GATEWAY_TOKEN_VAR] = gateway.auth_token
    return child


ReviewRunner = Callable[[CiReviewPlan, dict[str, str]], tuple[int, str]]
"""Runs ``repoach review pr`` for a plan in an environment; returns ``(rc, stdout)``."""

PrStateReader = Callable[[CiReviewPlan], str]
"""Reads the PR's state (``open``, ``merged``, ``closed``…); raises ``ForgeError`` on failure."""

GatewayStarter = Callable[[Mapping[str, str]], AbstractContextManager[GatewayHandle]]
"""Starts the loopback gateway for the ``with`` block (direct mode)."""


def subprocess_review_runner(cwd: Path, python: str = sys.executable) -> ReviewRunner:
    """Return a runner that calls ``repoach review pr`` in a child interpreter.

    The child's stderr is the job log; its stdout is captured for the JSON.

    Args:
        cwd: The checkout the review runs in.
        python: The interpreter with repoach installed.
    """

    def run(plan: CiReviewPlan, env: dict[str, str]) -> tuple[int, str]:
        argv = [python, "-m", "repoach.cli.main", "review", "pr", str(plan.pr_number)]
        argv += ["--forge", plan.forge]
        if plan.mode == "dry-run":
            argv.append("--dry-run")
        completed = subprocess.run(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, text=True, check=False
        )
        return completed.returncode, completed.stdout

    return run


def _parse_review_json(stdout: str) -> dict[str, object] | None:
    """Return the review's JSON object, or None when stdout holds none.

    ``review pr`` prints exactly one JSON document on stdout
    (SP-CLI-STDOUT-ONE-JSON); anything else is logged and read as no
    verdict.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError as exc:
        _log.warning("ci_review_stdout_not_json", error=str(exc), head=text[:200])
        return None
    return payload if isinstance(payload, dict) else None


def _count(payload: dict[str, object] | None, key: str) -> int:
    """Return ``payload[key]`` as an int, 0 when absent or not an int."""
    if payload is None:
        return 0
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def interpret_review(
    plan: CiReviewPlan, rc: int, stdout: str, provider: ProviderMode
) -> CiReviewReport:
    """Turn ``repoach review pr``'s exit code and JSON into the run's outcome.

    ``review pr`` exits 2 both for ``REQUEST_CHANGES`` and for a provider
    or forge it could not use; only the JSON tells them apart.
    """
    payload = _parse_review_json(stdout)
    raw_verdict = payload.get("final_verdict") if payload is not None else None
    verdict = raw_verdict if isinstance(raw_verdict, str) else None
    fields: dict[str, object] = {
        "forge": plan.forge,
        "forge_source": plan.forge_source,
        "pr_number": plan.pr_number,
        "pr_source": plan.pr_source,
        "mode": plan.mode,
        "provider_mode": provider,
        "final_verdict": verdict,
        "posted_comments": _count(payload, "posted_comments"),
        "posted_reviews": _count(payload, "posted_reviews"),
        "review_exit_code": rc,
        "review": payload,
    }
    if (rc == 0 and verdict is not None) or (rc == 2 and verdict == "REQUEST_CHANGES"):
        return finish(Outcome.REVIEWED, f"advisory: {verdict}", strict=plan.strict, **fields)
    if rc == 3:
        return finish(
            Outcome.REVIEWER_ERROR,
            "a reviewer reached no model (verdict ERROR); nothing was posted",
            strict=plan.strict,
            **fields,
        )
    if rc == 2:
        return finish(
            Outcome.UNAVAILABLE,
            "repoach could not review (exit 2, no verdict: provider or forge unavailable)",
            strict=plan.strict,
            **fields,
        )
    return finish(
        Outcome.INTERNAL_ERROR, f"repoach review pr exited {rc}", strict=plan.strict, **fields
    )


def run_ci_review(
    plan: CiReviewPlan,
    env: Mapping[str, str],
    *,
    pr_state: PrStateReader,
    review_runner: ReviewRunner,
    gateway_starter: GatewayStarter,
    bot_username: str | None = None,
) -> CiReviewReport:
    """Run the review a plan describes and report its outcome.

    Args:
        plan: The resolved inputs.
        env: The job environment.
        pr_state: Reads the PR state before anything else (the merge race).
        review_runner: Runs ``repoach review pr``.
        gateway_starter: Starts the loopback gateway (direct mode).
        bot_username: The forge's effective bot identity; an empty one
            refuses posting on GitLab, where no marker could be trusted.

    Returns:
        The final report.
    """
    common: dict[str, object] = {
        "forge": plan.forge,
        "forge_source": plan.forge_source,
        "pr_number": plan.pr_number,
        "pr_source": plan.pr_source,
        "mode": plan.mode,
        "provider_mode": plan.provider_mode,
    }
    if plan.mode == "post" and plan.forge == "gitlab" and not bot_username:
        return finish(
            Outcome.CONFIG_ERROR,
            "bot username required to trust the archive marker: set [review].bot_username "
            "or REPOACH_REVIEW_BOT_LOGIN before posting on GitLab",
            strict=plan.strict,
            **common,
        )
    try:
        state = pr_state(plan)
    except ForgeError as exc:
        return finish(
            Outcome.UNAVAILABLE,
            f"could not read the state of #{plan.pr_number}: {exc}",
            strict=plan.strict,
            **common,
        )
    if state == "merged":
        return finish(
            Outcome.ALREADY_MERGED,
            f"#{plan.pr_number} is already merged",
            strict=plan.strict,
            **common,
        )
    if state == "closed":
        return finish(Outcome.CLOSED, f"#{plan.pr_number} is closed", strict=plan.strict, **common)
    if plan.provider_mode == "gateway":
        rc, stdout = review_runner(plan, review_env(plan, env, None))
        return interpret_review(plan, rc, stdout, "gateway")
    try:
        with gateway_starter(env) as gateway:
            rc, stdout = review_runner(plan, review_env(plan, env, gateway))
    except GatewayStartError as exc:
        return finish(
            Outcome.UNAVAILABLE,
            f"the loopback gateway did not start: {exc}",
            strict=plan.strict,
            **common,
        )
    return interpret_review(plan, rc, stdout, "direct")


def apply_forge_env(plan: CiReviewPlan, env: Mapping[str, str]) -> None:
    """Export the derived forge variables into this process (for the PR-state read)."""
    for key, value in forge_env(plan, env).items():
        os.environ.setdefault(key, value)
