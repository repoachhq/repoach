"""``repoach ci review``: an advisory review from any CI, with one semantics.

Absorbs Yoob's ``ci/review-advisory.sh`` (2026-10-18) into repoach so no
project carries a shell driver:

* a missing input is a named skip, never a red job: no pull request
  (not a merge-request pipeline), no forge token, no provider;
* nothing is posted unless ``REPOACH_ADVISORY_POST`` is exactly ``1``
  (or ``--post``); every other value runs the review with ``--dry-run``;
* four provider modes: **claude_code** / **anthropic** (an explicit
  ``REPOACH_PROVIDER`` or ``[provider].kind``: the review calls the
  model in-process, no gateway, SP-UNIVERSAL-PROVIDERS), **gateway**
  (``REPOACH_LLM_PROXY_BASE_URL`` + ``REPOACH_ANTHROPIC_AUTH_TOKEN``, a
  gateway already running) and **direct** (only
  ``REPOACH_ANTHROPIC_API_KEY`` and no kind: a loopback gateway is
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
import signal
import socket
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
from ..forge import ForgeError, WriteRefusal
from ..project.config import ForgeName
from ..providers.errors import NO_PROVIDER_MESSAGE
from ..providers.select import PRESETS, provider_credential_names, provider_needs_key
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
    FORCE_VAR,
    GATEWAY_TOKEN_VAR,
    GATEWAY_URL_VAR,
    GITHUB_TOKEN_VARS,
    GITLAB_TOKEN_VARS,
    POST_VAR,
    PROVIDER_VAR,
)

_log = get_logger(__name__)

ReviewMode = Literal["post", "dry-run"]
ProviderMode = Literal[
    "gateway", "direct", "claude_code", "anthropic", "openai_compatible", "gemini", "none"
]
PlannedProviderMode = Literal[
    "gateway", "direct", "claude_code", "anthropic", "openai_compatible", "gemini"
]

IN_PROCESS_MODES: tuple[PlannedProviderMode, ...] = (
    "claude_code",
    "anthropic",
    "openai_compatible",
    "gemini",
)
"""Modes whose review calls the model itself: no gateway, no loopback."""


EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_CONFIG = 2
EXIT_REVIEWER_ERROR = 3
EXIT_CHANGES_REQUESTED = 4

REVIEW_EXIT_WRITE_REFUSED = 8
"""``repoach review pr``'s exit code when a dry run reached the forge write barrier."""

EXIT_CODE_TABLE: tuple[tuple[int, str], ...] = (
    (EXIT_OK, "advisory mode: always; --strict: reviewed (APPROVE/COMMENT), merged or closed"),
    (EXIT_INTERNAL, "--strict: internal error (unexpected exit code or exception)"),
    (EXIT_CONFIG, "--strict: skipped for a missing input, bad flag/config, forge/provider down"),
    (EXIT_REVIEWER_ERROR, "--strict: a reviewer reached no model, or the review timed out"),
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
    TIMEOUT = "timeout"
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
        provider_mode: ``claude_code``, ``anthropic``, ``gateway``,
            ``direct`` (loopback gateway) or ``none``.
        provider: The provider selected — the kind or preset named
            (``ollama``, ``gemini``…), else the mode (``gateway``,
            ``direct``) — also when its key is missing; None only when
            no provider is named or configured at all.
        final_verdict: The team verdict when a review ran.
        posted_comments: Comments the review posted (0 on a dry run).
        posted_reviews: Reviews the review posted (0 on a dry run).
        refused_writes: Writes the review's forge barrier refused; any
            value but 0 is a defect (SP-DRY-RUN-NEVER-POSTS).
        review_exit_code: ``repoach review pr``'s own exit code.
        review: The review's JSON payload, verbatim, when it printed one.
        detail: The first 200 characters of the review's stdout when it
            was not one JSON document.
        notes: Lines printed on stderr beside the outcome (agent memory off…).
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
    provider: str | None = None
    final_verdict: str | None = None
    posted_comments: int = 0
    posted_reviews: int = 0
    refused_writes: int = 0
    review_exit_code: int | None = None
    review: dict[str, object] | None = None
    detail: str | None = None
    notes: list[str] = []
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
        provider_mode: ``claude_code``, ``anthropic``, ``gateway`` or
            ``direct``.
        forge_token_var: The variable holding the forge token.
        strict: Whether ``--strict`` was given.
        provider_name: In an in-process mode, the provider the review
            child is told to use (a kind or a preset such as ``ollama``);
            empty otherwise.
        write_refusal: Why the review writes nothing in ``dry-run`` mode
            (``--dry-run``, or ``REPOACH_ADVISORY_POST`` not ``1``); None
            when it posts.
    """

    model_config = ConfigDict(frozen=True)

    forge: ForgeName
    forge_source: str
    pr_number: int
    pr_source: str
    mode: ReviewMode
    provider_mode: PlannedProviderMode
    forge_token_var: str
    strict: bool = False
    provider_name: str = ""
    write_refusal: WriteRefusal | None = None


def exit_code_for(outcome: Outcome, final_verdict: str | None, *, strict: bool) -> int:
    """Map an outcome to the exit code: 0 in advisory mode, the table under ``--strict``."""
    if not strict:
        return EXIT_OK
    if outcome == Outcome.REVIEWED:
        return EXIT_CHANGES_REQUESTED if final_verdict == "REQUEST_CHANGES" else EXIT_OK
    if outcome in (Outcome.ALREADY_MERGED, Outcome.CLOSED):
        return EXIT_OK
    if outcome in (Outcome.REVIEWER_ERROR, Outcome.TIMEOUT):
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


def dry_run_refusal(mode: ReviewMode, *, dry_run: bool) -> WriteRefusal | None:
    """Name why a ``dry-run`` review writes nothing; None when it posts.

    ``--dry-run`` is a dry run by request; any other ``dry-run`` comes
    from ``REPOACH_ADVISORY_POST`` not being exactly ``1``
    (SP-DRY-RUN-NEVER-POSTS).
    """
    if mode == "post":
        return None
    return WriteRefusal.DRY_RUN if dry_run else WriteRefusal.ADVISORY_POST_OFF


def provider_mode(env: Mapping[str, str], kind: str = "") -> tuple[ProviderMode, str | None]:
    """Choose the provider mode, or name what is missing.

    An explicit ``claude_code``, ``anthropic``, ``openai_compatible`` /
    preset or ``gemini`` kind wins: the review calls the model
    in-process.  Otherwise gateway mode wins when both its variables are
    set, and a lone direct key starts a loopback gateway.  Nothing at all
    is :data:`~repoach.providers.errors.NO_PROVIDER_MESSAGE`.

    Args:
        env: The job environment.
        kind: ``[provider].kind`` as resolved for the project; empty
            means read ``REPOACH_PROVIDER`` from ``env``.

    Returns:
        ``(mode, None)`` when usable, ``("none", reason)`` otherwise.
    """
    chosen_kind = kind or env_value(env, PROVIDER_VAR) or ""
    if chosen_kind == "claude_code":
        return "claude_code", None
    if chosen_kind == "anthropic":
        if first_set(env, DIRECT_KEY_VARS) is None:
            return "none", f"provider anthropic: {' / '.join(DIRECT_KEY_VARS)} is not set"
        return "anthropic", None
    if chosen_kind in PRESETS or chosen_kind in ("openai_compatible", "gemini"):
        names = provider_credential_names(chosen_kind)
        if provider_needs_key(chosen_kind) and first_set(env, names) is None:
            return "none", f"provider {chosen_kind}: {' / '.join(names)} is not set"
        return ("gemini" if chosen_kind == "gemini" else "openai_compatible"), None
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
    return "none", NO_PROVIDER_MESSAGE


def plan_ci_review(
    env: Mapping[str, str],
    *,
    pr_override: int | None = None,
    forge_override: ForgeName | None = None,
    project_forge: Callable[[], tuple[ForgeName, str]] | None = None,
    post: bool = False,
    dry_run: bool = False,
    strict: bool = False,
    provider_kind: str = "",
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
        provider_kind: The project's ``[provider].kind`` (see
            :func:`provider_mode`).

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
    chosen, missing = provider_mode(env, provider_kind)
    named = provider_kind or env_value(env, PROVIDER_VAR) or ""
    if chosen == "none":
        return finish(
            Outcome.SKIPPED, f"skipped, {missing}", strict=strict, provider=named or None, **common
        )
    return CiReviewPlan(
        provider_name=named if chosen in IN_PROCESS_MODES else "",
        forge=forge,
        forge_source=forge_source,
        pr_number=number,
        pr_source=pr_source,
        mode=mode,
        provider_mode=chosen,
        forge_token_var=token_var,
        strict=strict,
        write_refusal=dry_run_refusal(mode, dry_run=dry_run),
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


MEMORY_SWITCHES: tuple[str, ...] = (
    "REPOACH_REVIEW_MEMORY_ENABLED",
    "REPOACH_BUILDER_MEMORY_ENABLED",
    "REPOACH_REVIEW_LESSONS_ENABLED",
)
"""The agent-memory switches a review reads (recall, builder loop, lesson distillation)."""

MEMORY_URL_VARS: tuple[str, ...] = ("REPOACH_AGENTMEMORY_URL", "AGENTMEMORY_URL")
DEFAULT_MEMORY_URL = "http://localhost:3111"


def memory_reachable(url: str, timeout_s: float = 1.0) -> bool:
    """True when a TCP connection to the agent-memory URL's host and port opens."""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError as exc:
        _log.debug("ci_memory_unreachable", url=url, error=str(exc))
        return False


def memory_off_note(env: Mapping[str, str], reachable: Callable[[str], bool]) -> str | None:
    """Say why agent memory is turned off for the review, None to leave it alone.

    The agent-memory service (``localhost:3111``) is a developer-machine
    service: a CI job has none, and each recall would wait for its
    timeout. Memory is turned off when the job is a CI (``CI=true``,
    ``GITLAB_CI``, ``GITHUB_ACTIONS``) or the URL does not answer, unless
    the operator set one of :data:`MEMORY_SWITCHES` explicitly.
    """
    if first_set(env, MEMORY_SWITCHES) is not None:
        return None
    if env_value(env, "CI") == "true" or detect_ci_forge(env)[0] is not None:
        return "agent memory off: CI job (the agentmemory service is a developer-machine service)"
    url = next((env_value(env, n) for n in MEMORY_URL_VARS if env_value(env, n)), None)
    target = url or DEFAULT_MEMORY_URL
    if not reachable(target):
        return f"agent memory off: {target} unreachable"
    return None


def review_env(
    plan: CiReviewPlan,
    env: Mapping[str, str],
    gateway: GatewayHandle | None,
    *,
    memory_off: bool = False,
) -> dict[str, str]:
    """Build the review's environment: the forge token in, the provider key out.

    In ``anthropic`` mode the review calls the API itself, so the key
    stays in and ``REPOACH_PROVIDER`` names the mode; in ``claude_code``
    mode no key exists and the CLI's own login (``HOME``,
    ``CLAUDE_CODE_OAUTH_TOKEN``) is inherited.

    Args:
        plan: The run plan.
        env: The job environment.
        gateway: The loopback gateway in direct mode, None otherwise.
        memory_off: Set every :data:`MEMORY_SWITCHES` to ``false``.

    Returns:
        The environment for ``repoach review pr``.
    """
    keep_key = plan.provider_mode == "anthropic"
    child = {k: v for k, v in env.items() if keep_key or k not in DIRECT_KEY_VARS}
    if plan.provider_mode in IN_PROCESS_MODES:
        child[PROVIDER_VAR] = plan.provider_name or plan.provider_mode
    child.update(forge_env(plan, env))
    if memory_off:
        child.update(dict.fromkeys(MEMORY_SWITCHES, "false"))
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


DEFAULT_REVIEW_TIMEOUT_S = 25 * 60
"""Deadline of the review child; under a 30-minute job timeout, with room to report."""

KILL_GRACE_S = 10.0


class ReviewTimeoutError(Exception):
    """The review child outlived its deadline and was killed with its process group."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        super().__init__(f"the review did not finish within {timeout_s / 60:.0f} min")


def _kill_group(process: subprocess.Popen[str]) -> None:
    """Terminate the child's whole process group, then kill it after a grace period."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError as exc:
        _log.debug("ci_review_group_gone", pid=process.pid, error=str(exc))
        return
    try:
        process.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired as exc:
        _log.warning("ci_review_group_kill", pid=process.pid, error=str(exc))
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def subprocess_review_runner(
    cwd: Path, python: str = sys.executable, timeout_s: float = DEFAULT_REVIEW_TIMEOUT_S
) -> ReviewRunner:
    """Return a runner that calls ``repoach review pr`` in a child interpreter.

    The child's stderr is the job log; its stdout is captured for the
    JSON. It runs in its own session, so a timeout kills it together with
    everything it started (``gh``, ``glab``, verifier tools).

    Args:
        cwd: The checkout the review runs in.
        python: The interpreter with repoach installed.
        timeout_s: Deadline before the process group is killed.

    Raises:
        ReviewTimeoutError: From the runner, when the deadline passes.
    """

    def run(plan: CiReviewPlan, env: dict[str, str]) -> tuple[int, str]:
        argv = [python, "-m", "repoach", "review", "pr", str(plan.pr_number)]
        argv += ["--forge", plan.forge]
        if plan.mode == "dry-run":
            argv.append("--dry-run")
            if plan.write_refusal is not None:
                argv += ["--dry-run-reason", plan.write_refusal.value]
        if env.get(FORCE_VAR, "").strip() == "1":
            argv.append("--force")
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, text=True, start_new_session=True
        )
        try:
            stdout, _ = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            _log.warning("ci_review_timeout", pid=process.pid, timeout_s=timeout_s, error=str(exc))
            _kill_group(process)
            process.communicate()
            raise ReviewTimeoutError(timeout_s) from exc
        return process.returncode, stdout

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
        "provider": plan.provider_name or provider,
        "final_verdict": verdict,
        "posted_comments": _count(payload, "posted_comments"),
        "posted_reviews": _count(payload, "posted_reviews"),
        "refused_writes": _count(payload, "refused_writes"),
        "review_exit_code": rc,
        "review": payload,
        "detail": stdout.strip()[:200] if payload is None and stdout.strip() else None,
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
    if rc == REVIEW_EXIT_WRITE_REFUSED:
        return finish(
            Outcome.INTERNAL_ERROR,
            "the dry run attempted a forge write; the barrier refused it, nothing was sent",
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
    memory_probe: Callable[[str], bool] = memory_reachable,
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
        memory_probe: Says whether the agent-memory URL answers.

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
        "provider": plan.provider_name or plan.provider_mode,
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
    if state == "unknown":
        return finish(
            Outcome.SKIPPED,
            f"skipped, the forge reports state 'unknown' for #{plan.pr_number}",
            strict=plan.strict,
            **common,
        )
    note = memory_off_note(env, memory_probe)
    notes = [note] if note is not None else []
    off = note is not None
    try:
        if plan.provider_mode != "direct":
            rc, stdout = review_runner(plan, review_env(plan, env, None, memory_off=off))
        else:
            with gateway_starter(env) as gateway:
                child = review_env(plan, env, gateway, memory_off=off)
                rc, stdout = review_runner(plan, child)
    except GatewayStartError as exc:
        return finish(
            Outcome.UNAVAILABLE,
            f"the loopback gateway did not start: {exc}",
            strict=plan.strict,
            **common,
        )
    except ReviewTimeoutError as exc:
        return finish(Outcome.TIMEOUT, f"{exc}; killed", strict=plan.strict, notes=notes, **common)
    report = interpret_review(plan, rc, stdout, plan.provider_mode)
    return report.model_copy(update={"notes": notes})


def apply_forge_env(plan: CiReviewPlan, env: Mapping[str, str]) -> None:
    """Export the derived forge variables into this process (for the PR-state read)."""
    for key, value in forge_env(plan, env).items():
        os.environ.setdefault(key, value)
