# Security Policy

## Supported Versions

| Branch | Status |
| --- | --- |
| `main` | Released. Security fixes land here first via a `develop` → `main` release PR, or as a direct hot-fix for a critical issue. |
| `develop` | Active development / integration branch. Pre-release; fixes merge here before promotion to `main`. |

There are no numbered release lines yet (see `SP-PYPI-RELEASE` in
`docs/specs/`): "supported" means "the two branches above", not a set
of tagged versions.

## Reporting a Vulnerability

Preferred channel: GitHub's private vulnerability reporting
(Security tab → **Report a vulnerability**), which opens a draft
security advisory visible only to maintainers.

**Current state:** private vulnerability reporting is **not yet
enabled** on this repository (`gh api repos/repoachhq/repoach --jq
.security_and_analysis`, checked 2026-10-26, confirms nothing
indicates it is on; the dedicated endpoint
`repos/repoachhq/repoach/private-vulnerability-reporting` returns
`{"enabled":false}`). A maintainer can turn it on from **Settings →
Security → Code security and analysis → Private vulnerability
reporting → Enable**. Until it is enabled, do not open a public
issue for a security report — instead open a draft GitHub Security
Advisory directly from the Security tab (**Advisories → New draft
security advisory**), which is available independently of the
repository-level toggle, or mention `@jwfaye` in a GitHub
conversation marked private.

Please include:

- the affected version or commit SHA (`main` or `develop`),
- steps to reproduce, or a minimal proof of concept,
- the impact you believe it has.

**Response time:** reports will be acknowledged within 7 days.

## Scope

In scope:

- the LLM proxy (`src/repoach/llm_proxy`) and how provider API keys
  reach or avoid it,
- the forge's write barrier — the mechanism that must keep a dry run
  or an unauthorized actor from posting, commenting, or pushing
  (`SP-DRY-RUN-NEVER-POSTS`, `src/repoach/forge/protocol.py`),
- secrets isolation between PR-authored code and the jobs that hold
  provider keys or a write-scoped `GITHUB_TOKEN`
  (`SP-CI-SECRETS-ISOLATION`, `.github/workflows/auto-review.yml`).

See [`docs/security_posture.md`](docs/security_posture.md) for the
current implementation detail of these controls (action pinning,
actor allowlists, environment-scoped secrets, and the branch
protection upgrade path) and its residual-trust design.

Out of scope: vulnerabilities in third-party dependencies (report
upstream), and social-engineering or physical-access attacks.
