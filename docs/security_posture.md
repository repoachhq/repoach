# Security posture — CI supply chain & branch protection

Closes **SP-CI-SUPPLY-CHAIN-HARDEN** G3 (documentation goal). G1 (pin every
third-party action to an immutable commit SHA) and G2 (gate `ci.yml`'s
PR-triggered job to the maintainer allowlist) are already implemented in the
workflows themselves; this document records the residual-trust design, the
compensating controls, and — importantly — how the branch-protection gap the
original spec described has since become closeable server-side.

## Residual-trust design (unchanged — SP-CI-SUPPLY-CHAIN-HARDEN NG1)

Pull-request CI runs on a **self-hosted runner** so the review-bot team can
work without burning Anthropic quota. Untrusted PR code therefore executes on
the maintainer's machine. Two controls contain that trust:

1. **Actor allowlist.** Every job that runs PR code — the `ci.yml` `test`
   matrix and the `auto-review.yml` review / `auto_fix` jobs — is gated by
   `contains(fromJSON('["jwfaye"]'), github.actor)` on `pull_request`
   events. A PR from any other actor does not execute code on the runner.
   (`push` / `workflow_dispatch` on protected branches are already trusted.)
2. **Environment-scoped secrets.** Provider keys live in the `bots`
   GitHub Environment, not in repo-wide secrets, so they are only exposed to
   the jobs that declare `environment: bots`.

## G1 — action pinning (done)

Every third-party action in `ci.yml` and `auto-review.yml` (and
`merge-on-ci.yml`, `shellcheck.yml`) is pinned to a full-length commit SHA
with the human-readable version in a trailing comment, e.g.:

```yaml
uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4.3.1
```

`grep -rE 'uses: .*@(v[0-9]|main|master)' .github/workflows/` returns nothing:
no mutable major tag remains. A tag-move supply-chain attack (repointing `@v4`
upstream) therefore cannot alter what runs on the self-hosted runner — the SHA
is immutable.

## G2 — `ci.yml` actor gate (done)

`ci.yml`'s `test` job carries the same actor gate as the review workflow:

```yaml
if: >-
  github.event_name != 'pull_request'
  || contains(fromJSON(vars.REPOACH_ALLOWED_ACTORS || '["jwfaye"]'), github.actor)
```

so an untrusted actor cannot run PR code on the self-hosted runner through CI,
matching `auto-review.yml`. The allowlist is sourced from the
`REPOACH_ALLOWED_ACTORS` repository variable (see *Fork portability* below);
the `'["jwfaye"]'` literal is only the maintainer-repo default that applies
when the variable is unset. `tests/unit/test_ci_portability.py` locks the
wiring so no workflow regresses to a hardcoded actor or runner.

## Fork portability — `REPOACH_RUNNER` and `REPOACH_ALLOWED_ACTORS`

`.github/workflows/*` is bot-forbidden, so its trust boundary must be
reconfigurable without editing the files. Two GitHub **repository variables**
(Settings → Secrets and variables → Actions → Variables) carry the
deployment-specific values; both fall back to the maintainer defaults so this
repo needs no configuration:

| Variable | Default when unset | Purpose |
| --- | --- | --- |
| `REPOACH_RUNNER` | `self-hosted` | Runner label every job targets. A fork on GitHub-hosted runners sets `ubuntu-latest`. |
| `REPOACH_ALLOWED_ACTORS` | `["jwfaye"]` | JSON array of logins allowed to run PR code on the runner. A fork sets its own maintainers, e.g. `["alice","bob"]`. |

A forker's entire trust setup is therefore two variables in the GitHub UI — no
workflow edits, which keeps the bot-forbidden files pristine. Set both together
before enabling PR CI: an empty or wrong `REPOACH_ALLOWED_ACTORS` on a
self-hosted runner would either lock the maintainer out or admit untrusted PR
execution.

## H2 — branch protection: the gap, and how it is now closeable

**When the spec was written (2026-07-13, private free plan):**
`gh api repos/repoachhq/repoach/branches/develop/protection` returned **403** —
server-side branch protection was not available on a private repo on the free
plan. `develop`/`main` protection was therefore **entirely client-side**:

- the workflow `if:` actor gates above, and
- `.githooks/pre-push`, which textually refuses a direct push to
  `develop`/`main`.

Both are **bypassable**: the pre-push hook is skipped by `git push --no-verify`,
and the actor gate only constrains *PR* execution, not what a trusted actor
pushes directly. A trusted-but-mistaken push could still reach a protected
branch.

**Now (public since 2026-07-19):** the same API call returns **404 "Branch not
protected"**, not 403. On a **public** repository, GitHub Free *does* offer
branch-protection rules — so the gap is no longer a plan limitation. It is
simply **unconfigured**. `private: false, visibility: public` is confirmed via
`gh api repos/repoachhq/repoach`.

### Upgrade path (operator action — SP-CI-SUPPLY-CHAIN-HARDEN NG3)

Server-side protection is now available and *recommended*; enabling it is an
outward-facing operator decision (it changes how every merge is enforced), so
it is documented here rather than applied in code. To restore real server-side
enforcement on `develop` and `main`:

- **Require status checks to pass** before merging — the `Test suite (Python
  3.11)` and `Test suite (Python 3.13)` CI jobs (and `ShellCheck`). This makes
  the green-CI merge gate server-enforced instead of convention.
- **Require a pull request** before merging (no direct pushes) — this is what
  the client-side pre-push hook approximates today, made unbypassable.
- **Restrict who can push** to the protected branches.
- Keep bots barred from auto-merging into `main` (they only auto-merge
  `develop` on APPROVE + green CI); server-side protection makes that a hard
  guarantee, not a policy.

Until that is enabled, the client-side controls above remain the only
enforcement, with the `--no-verify` bypass an accepted, documented residual
risk for the single trusted maintainer.

## Scope notes

- `.github/workflows/*` is path-whitelist-forbidden for every fix the review
  bots emit (see CLAUDE.md); workflow changes are hand-applied by the operator.
- This document is the security/ops reference the spec's G3 calls for; keep the
  action-pinning and actor-allowlist claims above in sync with the workflows if
  either changes.
