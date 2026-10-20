"""The CI variables ``repoach ci review`` reads, by name and purpose.

One table drives both the command and ``repoach doctor --ci``, so the
doctor can never list a variable the command ignores or miss one it
reads. Values are never printed by anything that consumes this table.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

GITLAB_TOKEN_VARS: tuple[str, ...] = ("GITLAB_TOKEN", "REPOACH_GITLAB_TOKEN")
"""Token ``glab`` posts and reads with, first found wins."""

GITHUB_TOKEN_VARS: tuple[str, ...] = ("GH_TOKEN", "GITHUB_TOKEN")
"""Token ``gh`` posts and reads with, first found wins."""

GATEWAY_URL_VAR = "REPOACH_LLM_PROXY_BASE_URL"
"""Gateway mode: the base URL of a running repoach gateway."""

GATEWAY_TOKEN_VAR = "REPOACH_ANTHROPIC_AUTH_TOKEN"
"""Gateway mode: the gateway's shared secret."""

DIRECT_KEY_VARS: tuple[str, ...] = ("REPOACH_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
"""The Anthropic key: read in-process in ``anthropic`` mode, else handed to a loopback gateway."""

PROVIDER_VAR = "REPOACH_PROVIDER"
"""``claude_code`` / ``anthropic``: the review calls the model itself (SP-UNIVERSAL-PROVIDERS)."""

CLAUDE_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
"""``claude_code`` mode in CI: the token ``claude setup-token`` prints (no interactive login)."""

POST_VAR = "REPOACH_ADVISORY_POST"
"""Posting switch: only the exact value ``1`` posts; anything else is a dry run."""

MODEL_VARS: tuple[str, ...] = ("REPOACH_MODEL_OPUS", "REPOACH_MODEL_SONNET", "REPOACH_MODEL_HAIKU")
"""Direct-provider mode: per-tier Anthropic model overrides."""

GATEWAY_PORT_VAR = "REPOACH_GATEWAY_PORT"
"""Direct-provider mode: loopback port override (a free port otherwise)."""

FORGE_TOKEN_VARS: tuple[str, ...] = (*GITLAB_TOKEN_VARS, *GITHUB_TOKEN_VARS, "CI_JOB_TOKEN")
"""Every forge credential name ``ci review`` itself knows about."""

PROXY_VARS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
)
"""Outbound proxy settings a scrubbed step keeps, so it still reaches the network."""

PASSTHROUGH_VARS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "PYTHONHOME",
    "Python_ROOT_DIR",
    "Python3_ROOT_DIR",
    "LD_LIBRARY_PATH",
    "pythonLocation",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    *PROXY_VARS,
)
"""The allow-list a scrubbed environment starts from: no credential is on it.

A CI injects credentials under names repoach cannot enumerate
(``CI_REGISTRY_PASSWORD``, ``CI_DEPENDENCY_PROXY_PASSWORD``,
``CI_JOB_JWT_V2``, ``ACTIONS_RUNTIME_TOKEN``,
``ACTIONS_ID_TOKEN_REQUEST_TOKEN``, any masked project variable), so the
loopback gateway and the install steps keep only these names plus the
one secret each needs, never "everything but a deny-list".

The list also keeps what an interpreter needs to exec at all: a Python
provisioned by ``actions/setup-python`` links ``libpython3.x.so`` through
``LD_LIBRARY_PATH`` (``pythonLocation``, ``Python_ROOT_DIR``) and dies
with exit 127 without it (PR #217 CI). ``LD_PRELOAD`` is deliberately
absent: it injects code into every process the step starts."""

PIP_PREFIX = "PIP_"
"""The install step also keeps ``PIP_*`` (``PIP_INDEX_URL`` and friends), so a
private index reaches pip; no other step sees them."""


class CiVariable(BaseModel):
    """One variable ``repoach ci review`` reads.

    Attributes:
        name: The environment variable.
        group: What it serves (``detect``, ``pr``, ``forge``, ``provider``,
            ``posting``, ``model``).
        purpose: One line, shown by ``doctor --ci``.
        secret: True when the value is a credential.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    group: str
    purpose: str
    secret: bool = False


CI_VARIABLES: tuple[CiVariable, ...] = (
    CiVariable(name="GITLAB_CI", group="detect", purpose="GitLab CI is running this job"),
    CiVariable(name="GITHUB_ACTIONS", group="detect", purpose="GitHub Actions is running this job"),
    CiVariable(name="CI_SERVER_URL", group="detect", purpose="GitLab instance (sets GITLAB_HOST)"),
    CiVariable(name="CI_PROJECT_ID", group="detect", purpose="GitLab project (reported)"),
    CiVariable(
        name="GITHUB_REPOSITORY", group="detect", purpose="GitHub owner/repo (sets GH_REPO)"
    ),
    CiVariable(name="REPOACH_PR_NUMBER", group="pr", purpose="explicit PR / MR number"),
    CiVariable(name="CI_MERGE_REQUEST_IID", group="pr", purpose="GitLab merge request iid"),
    CiVariable(name="GITHUB_EVENT_PATH", group="pr", purpose="GitHub event payload"),
    CiVariable(name="GITHUB_REF", group="pr", purpose="GitHub refs/pull/<n>/merge"),
    *(
        CiVariable(name=name, group="forge", purpose="GitLab token (glab)", secret=True)
        for name in GITLAB_TOKEN_VARS
    ),
    *(
        CiVariable(name=name, group="forge", purpose="GitHub token (gh)", secret=True)
        for name in GITHUB_TOKEN_VARS
    ),
    CiVariable(
        name=PROVIDER_VAR,
        group="provider",
        purpose="claude_code or anthropic: call the model in-process, no gateway",
    ),
    CiVariable(
        name=CLAUDE_TOKEN_VAR,
        group="provider",
        purpose="claude_code mode: token from 'claude setup-token'",
        secret=True,
    ),
    CiVariable(name=GATEWAY_URL_VAR, group="provider", purpose="gateway mode: gateway base URL"),
    CiVariable(
        name=GATEWAY_TOKEN_VAR, group="provider", purpose="gateway mode: shared secret", secret=True
    ),
    *(
        CiVariable(
            name=name, group="provider", purpose="direct mode: Anthropic API key", secret=True
        )
        for name in DIRECT_KEY_VARS
    ),
    CiVariable(name=POST_VAR, group="posting", purpose="1 posts; anything else is a dry run"),
    *(
        CiVariable(name=name, group="model", purpose="direct mode: model id override")
        for name in MODEL_VARS
    ),
    CiVariable(name=GATEWAY_PORT_VAR, group="model", purpose="direct mode: loopback port"),
)
"""Every variable ``repoach ci review`` reads, in display order."""
