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
   `contains(fromJSON(vars.REPOACH_ALLOWED_ACTORS || '[]'), github.actor)` on `pull_request`
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
  || contains(fromJSON(vars.REPOACH_ALLOWED_ACTORS || '[]'), github.actor)
```

so an untrusted actor cannot run PR code on the self-hosted runner through CI,
matching `auto-review.yml`. The allowlist is sourced from the
`REPOACH_ALLOWED_ACTORS` repository variable (see *Fork portability* below);
the `'[]'` fallback is empty, so the workflows run no pull-request job
until it is set. `tests/unit/test_ci_portability.py` locks the
wiring so no workflow regresses to a hardcoded actor or runner.

## Fork portability — `REPOACH_RUNNER` and `REPOACH_ALLOWED_ACTORS`

`.github/workflows/*` is bot-forbidden, so its trust boundary must be
reconfigurable without editing the files. Two GitHub **repository variables**
(Settings → Secrets and variables → Actions → Variables) carry the
deployment-specific values. `REPOACH_RUNNER` falls back to `self-hosted`;
`REPOACH_ALLOWED_ACTORS` is required: the workflows run no pull-request job
until it is set:

| Variable | Default when unset | Purpose |
| --- | --- | --- |
| `REPOACH_RUNNER` | `self-hosted` | Runner label every job targets. A fork on GitHub-hosted runners sets `ubuntu-latest`. |
| `REPOACH_REVIEW_RUNNER` | `REPOACH_RUNNER` | Runner label for the `review` and `auto_merge` jobs of `auto-review.yml` only, so reviews do not queue behind CI. `auto_fix` stays on `REPOACH_RUNNER`: it executes PR code. The review runner receives the `bots` provider keys and a `contents: write` token, so register it with only its own label (`--no-default-labels`) and never let it serve a job that runs PR code. |
| `REPOACH_ALLOWED_ACTORS` | `[]` (required: the workflows run no pull-request job until it is set) | JSON array of logins allowed to run PR code on the runner. Each repository sets its own maintainers, e.g. `["alice","bob"]`. |

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

## Personal data in fixtures and docs

repoach is for everyone, so no maintainer's path, login, host, address or
mailbox belongs in code, defaults, docs, fixtures, bench results, oracles or
shipped artefacts (SP-REPO-CARRIES-NO-PERSONAL-DATA). Every future fixture,
bench result, doc and spec follows one rule (`format:redaction-rule`,
SP-SCRUB-PERSONAL-DATA-AT-HEAD D1). An oracle that forbids a personal string
never spells it: it runs `scripts/lint_no_personal_data.py --tree <path>`
(SP-NO-PERSONAL-DATA-GATE), which detects the classes below and reads the
strings no class can detect from a per-maintainer deny-list kept outside the
tree.

| Class | Replacement | Note |
|---|---|---|
| absolute home path (`/home/<x>/…`, `/Users/<x>/…`) | `~/…` in prose; `<repo>/…` when the path is a checkout; `/path/to/…` in a recorded JSON value | never the `%h` specifier followed by a layout either |
| a maintainer's directory layout under the home | `<layout>`; in a unit, the render token `@REPOACH_HOME@` (`deploy/systemd/README.md`) | |
| system login, service account | `<user>`; in a recorded JSON value, the class word (`maintainer`, `author`, `bot`, `agent`, `service`) | |
| forge handle in a fixture or test | the class word with a stable suffix when several are needed: `maintainer`, `author`, `author-2`, `bot`, `service` | GitHub `login`, GitLab `username`, a login in argv and an `@<handle>` in a recorded body alike; a `<name>[bot]` login shares one consistent class word across its login, `@<name>` mention and bare prose stem; a hand-written placeholder (`alice`, `owner`…) is a real account and gets a class word too; only `github-actions[bot]` (the platform's) and `repoach-bot` (this project's) name no person and stay; a namespace inside a recorded URL is `org` (row below) |
| real name in a recorded user object | dropped: the `name` key is removed from the user object | `login`/`username` stay as class words; `name` elsewhere (check runs, jobs) is read by the forges and kept |
| user id in a recorded user object (REST id, node id) | a synthetic number in `1001..1999`, `U_<n>` for a node id, declared by the fixture under `synthetic_user_ids`; a declared number outside that range is not trusted and is remapped | the key is kept (GitLab reads the approver's id as `Review.id`); a sequence, never a hash, since a hash of a small id is reversed by enumeration; the maintainer's real ids are judged by the deny-list, never committed |
| recorded forge URL (`url`, `web_url`, `html_url`, `link`, and a URL or an scp/`ssh://` remote in recorded text) | `https://forge.example.com/org/project/…`: the host becomes `forge.example.com`, the namespace `org`, the repository `project` (a `.git` suffix kept, and a repository object's `name`/`path` by key, its `description` dropped; numeric `api/v4/projects/<id>/…` ids map to `2001..2999`), the rest of the path is kept, the query dropped and the fragment too unless it is a comment anchor; a userinfo is never kept (`git@` for a remote that had one, `git@forge.example.com:org/project` for an scp remote); a user object's own URLs are dropped; Markdown delimiters (`` ` ``, `*`, `|`, boundary `~`, `_`) terminate text URLs without being eaten; well-known public reference hosts (`docker.io`, `ghcr.io`, `golang.org`, `k8s.io`, `pypi.org`, `npmjs.com`, `docs.github.com`, `docs.gitlab.com`) stay intact in prose | applied at record time by `scripts/record_forge_fixture.py`, re-applied offline by `scripts/redact_forge_fixtures.py` (SP-RECORDER-REDACTS-BY-CLASS, covering `kwargs` as well as `args` and `result`); a recorded fixture's `source` names the forge, never the checkout's folder |
| hostname (short or FQDN) | `<host>`; in a URL, `example.com` or a subdomain of it | RFC 2606 |
| public IPv4 | `203.0.113.<n>` (RFC 5737 TEST-NET-3) | `192.0.2.<n>` and `198.51.100.<n>` are also accepted |
| public IPv6 | `2001:db8::<n>` (RFC 3849) | |
| mailbox | `<user>@example.com`; a forge noreply (`@users.noreply.github.com`, `@users.noreply.gitlab.com`, self-hosted `@users.noreply.<host>`) has its local part and host redacted by class (`author@users.noreply.forge.example.com` / `bot@users.noreply.forge.example.com`); a system noreply (`noreply@github.com`, `noreply@<self-hosted>`) becomes `noreply@forge.example.com` | |
| a domain of the owner's other product | `example.com`, or `product.example.com` when the sentence needs a product | the product's name alone is not personal data |

The real-account rule (SP-RECORDER-REDACTS-BY-CLASS D2): wherever a string
stands for an actor — a login value, a login in argv, a mention, the bot login
— it is a class word, whatever it was, since every short placeholder is
someone's account. A URL namespace a parser test writes from scratch on a real
forge host (`o/r`, `owner/repo`, `acme/…`) names no actor and stays, as host
detection needs the real host; one derived from a real account or project is
replaced by `group/project`. A login known only from an `@<handle>` mention is
replaced in that form only; a dotted `@a.b`, a scoped package (`@scope/pkg`)
and a standard-library decorator or annotation (`@dataclass`, `@Override`,
case-insensitive) are code, not mentions. A repository id in a recorded
repository object or `api/v4/projects/<id>/…` URL is a synthetic number in
`2001..2999`, declared under `synthetic_project_ids`. Both tools then judge
the fixture they are about to write with a closure oracle: the logins, hosts
(positive DNS TLD / known-host rule for schemeless hosts, plus scheme-led
IPv4 URLs such as `https://10.20.30.40/…`), namespace and repository names
and raw ids the recording saw, and the deny-list, searched for as whole
tokens (letters and digits; `-` and `_` delimit) in every string (including
`case`, `call["args"]`, and every key a forge does not write), casefolded,
URL/entity/`\u`-decoded, with embedded user node ids (`MDQ6VXNl…` and `U_…`)
read as their account number; one found is exit 2 and nothing is written. A
third party's project linked on a public forge (including under an ssh-alias
or empty `origin` remote when `URL_KEYS` identify the public forge) names no
one of the recording and is not searched for. The threat model is
**accidental leaks** from real pull requests, not adversarial evasion: a
login the recording never learns cited only as a bare word, schemeless
private IPv4 paths (`10.0.0.5/…`), Unicode confusables and invisible
characters, Markdown-split or escaped values, non-node-id base64 and
JSON-in-a-string escapes are out of scope (SP-RECORDER-REDACTS-BY-CLASS
Threat model & NG4).

Public attribution is not personal data: the copyright holder in `LICENSE`,
the `authors` of `pyproject.toml`, the security contact in `SECURITY.md`, the
maintainer column of `docs/languages.md` and a spec's `author:` line stay.
