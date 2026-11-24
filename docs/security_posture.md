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
| forge handle in a fixture or test | the class word with a stable suffix when several are needed: `maintainer`, `author`, `author-2`, `bot`, `service` | GitHub `login`, GitLab `username`, a login in argv and an `@<handle>` in a recorded body alike; a `<name>[bot]` login shares one consistent class word across its login and `@<name>` mention whatever the stem length (and its bare prose stem when ≥4 characters and outside class words and placeholders); a hand-written placeholder (`alice`, `owner`…) is a real account and gets a class word too; only `github-actions[bot]` (the platform's) and `repoach-bot` (this project's) name no person and stay; a namespace inside a recorded URL is `org` (row below) |
| real name in a recorded user object | dropped: the `name` key is removed from the user object | `login`/`username` stay as class words; `name` elsewhere (check runs, jobs) is read by the forges and kept |
| user id in a recorded user object (REST id, node id) | a synthetic number in `1001..1999`, `U_<n>` for a node id, declared by the fixture under `synthetic_user_ids`; a declared number outside that range is not trusted and is remapped | the key is kept (GitLab reads the approver's id as `Review.id`); a sequence, never a hash, since a hash of a small id is reversed by enumeration; the maintainer's real ids are judged by the deny-list, never committed |
| recorded forge URL (`url`, `web_url`, `html_url`, `link`, and a URL or an scp/`ssh://` remote in recorded text) | `https://forge.example.com/org/project/…`: the host becomes `forge.example.com`, the namespace `org`, the repository `project` (a `.git` suffix kept, and a repository object's `name`/`path` by key, its `description` dropped; a `projects/<n>` token with at least five digits names a project in every position and maps to `2001..2999`, four digits and fewer never do (`projects/2026/q1.md`, `/api/v4/projects/42`), the rest of the path is kept, the query dropped and the fragment too unless it is a comment anchor; a URL userinfo (a login, a credential, `${CI_JOB_TOKEN}`) is never kept and the whole URL is replaced, on `ssh`/`git`/SCP only the `git@` placeholder stays (`alice@example.com:org/repo.git` → `git@forge.example.com:org/project.git`, also on a unified-diff line); a protocol-relative `//host/path` is not recognised by shape at all: its host, namespace and repository are replaced as known values wherever they stand and the path is kept, so `//` code comments (`x := 1 //cfg.dev/env/prod`) stay byte-identical; a user object's own URLs are dropped; trailing prose punctuation (`.,:;!?)]}>'"`) and Markdown delimiters (`` ` ``, `*`, `|`, `~`, `_`) terminate text URLs without being eaten (GFM balanced parentheses stay inside URLs, trailing `_` inside a dropped fragment/query such as `#__init__` leaves no stray `__`, and SCP never eats a leading `_` delimiter); public documentation and package hosts are kept by a curated **registrable-suffix rule** (`PUBLIC_SUFFIXES` of `repoach.forge.closure_tables`: exact host or `.<suffix>`, covering `python.org`, `readthedocs.org`, `readthedocs.io`, `mozilla.org`, `crates.io`, `docs.rs`, `stackoverflow.com`, `wikipedia.org`, `pypi.org`, `npmjs.com`, `npmjs.org`, `go.dev`, `golang.org`, `docker.io`, `ghcr.io`, `k8s.io`, `docs.github.com`, `docs.gitlab.com`), with scheme and host lowercased, userinfo still stripped, and path/query still scanned for planted values (unlisted hosts are rewritten: over-redaction is the safe direction) | applied at record time by `scripts/record_forge_fixture.py`, re-applied offline by `scripts/redact_forge_fixtures.py` (SP-RECORDER-REDACTS-BY-CLASS, covering `args`, `kwargs`, `stdin`, `stderr`, JSON/non-JSON `stdout` and `result`); a recorded fixture's `source` names the forge, never the checkout's folder |
| hostname (short or FQDN) | `<host>`; in a URL, `example.com` or a subdomain of it | RFC 2606 |
| public IPv4 | `203.0.113.<n>` (RFC 5737 TEST-NET-3) | `192.0.2.<n>` and `198.51.100.<n>` are also accepted |
| public IPv6 | `2001:db8::<n>` (RFC 3849) | |
| mailbox | `<user>@example.com`; a forge noreply (`@users.noreply.github.com`, `@users.noreply.gitlab.com`, self-hosted `@users.noreply.<host>`) has its local part and host redacted by class (`author@users.noreply.forge.example.com` / `bot@users.noreply.forge.example.com`); a system noreply (`noreply@github.com`, `no-reply@<host>`, `noreply@<self-hosted>`) or `actions@github.com` becomes `noreply@forge.example.com` (`no-reply@forge.example.com` for `no-reply@`) | |
| a domain of the owner's other product | `example.com`, or `product.example.com` when the sentence needs a product | the product's name alone is not personal data |

The real-account rule (SP-RECORDER-REDACTS-BY-CLASS D2): wherever a string
stands for an actor — a login value, a login in argv, a mention, the bot login
— it is a class word, whatever it was, since every short placeholder is
someone's account. A URL namespace a parser test writes from scratch on a real
forge host (`o/r`, `owner/repo`, `acme/…`) names no actor and stays, as host
detection needs the real host; one derived from a real account or project is
replaced by `group/project`. A login known only from an `@<handle>` mention is
replaced in that form only; a dotted `@a.b`, a scoped package (`@scope/pkg`),
a standard-library decorator or annotation (`@dataclass`, `@Override`,
case-insensitive), a documentation tag (`@param`, `@link`, `@see`: JSDoc, Javadoc, PHPDoc, Doxygen) and a CSS, Sass or Tailwind at-rule (`@font-face`, `@media`) are code, not mentions — the 425 words of `LANGUAGE_AT_WORDS`; a mention of a non-participant whose login is one of them (`cc @test`) is therefore written in its `@` form (SP-RECORDER-REDACTS-BY-CLASS NG4-13), a participant's such login is a known value and is replaced everywhere. A real repository id in a recorded repository object or a `projects/<n>` token of at least five digits in any string is a synthetic number in `2001..2999`, declared under `synthetic_project_ids`; bare repository or file paths (`projects/2026/q1.md`, `/projects/2025/architecture.md`) and documentation examples (`gitlab.example.com/api/v4/projects/42`, `/api/v4/projects/1234`) are left intact. Since SP-RECORDER-REDACTS-BY-CLASS 0.17 the guarantee no longer rests on recognising shapes: every private value a recording holds in a structured field — host, namespace, repository, logins, numeric and node ids, a user's name and email — and every deny-list entry is a **known value**, replaced by its class token in every string of the fixture (letters and digits bound a token, `%2F` delimits), whatever the syntax around it; the shape recognisers above only make the placeholders read well. A separate closure oracle (`src/repoach/forge/closure.py` and its tables, `closure_tables.py`, sharing no regex or code object with the redactor, held so by a test) then refuses the fixture — exit 2, a reason, a kind, an ordinal and a path, never a value — when a known value survives (casefolded, in four rounds of percent, entity and string-escape decoding — `\uXXXX`, `\xHH` and the JSON `\/` — embedded user node ids read as their number), when a token is **host-shaped** and is neither a placeholder-class host (`forge.example.com`, RFC 2606, loopback, the unspecified and limited-broadcast addresses `0.0.0.0`, `255.255.255.255` and `::`, RFC 5737, the IPv6 documentation prefixes) nor on the public allowlist (the registrable suffixes of `PUBLIC_SUFFIXES` (`repoach.forge.closure_tables`), now also `pythonhosted.org`, `jsdelivr.net`, `unpkg.com`, `apache.org`, `nuget.org`, `yarnpkg.com`, `npmmirror.com`; a vendor that runs a tenant console or API under its suffix is public by exact host only, `PUBLIC_HOSTS`: the documentation, CDN, apex and `www` hosts of `cloudflare.com`, `mend.io` and `renovatebot.com`, never `dash.cloudflare.com/<account>` or `api.cloudflare.com/client/v4/accounts/<id>`, and a tenant route on a public host, Stack Overflow for Teams' `/c/<team>` (`TENANT_ROUTES`), is refused as a `tenant path` whatever leads its host (a scheme, `//`, nothing, a port, a Markdown link) and never kept by the redactor; a public forge host is never public here), when an IPv4 address that is led or followed is public (`104.16.0.1:8080`, `104.16.0.1/<ns>`; a bare quad, version or address, is ordinary text, and so is a quad in a version context: after a single `/` as a path segment, after a comparator, after a `version` label, or carrying a `-`/`+` tag) or an un-led global bracketed IPv6 literal appears, or when a `user@host` is neither a placeholder mailbox, nor `git@` on a placeholder-class host, nor a documentation-domain literal, nor a mailbox on a placeholder-class address (`root@127.0.0.1`), or when a URL userinfo (`<scheme>://<userinfo>@<host>`, any scheme) is not such a literal read as the mailbox `<userinfo>@<host>`: `git` on a placeholder-class host and a password-less user on a documentation host (`postgres://app@localhost`) pass, a credential (`u:abc=@`, `:pw!@`, `app:${DB_PASSWORD}@`) or any userinfo on a public or unknown host is refused as `url userinfo`, whatever characters it holds: the userinfo is read by URL grammar, everything between `//` and the last `@` of the authority, never by a character allowlist, so `app:<pw>^x@localhost` fails closed. Host, mailbox and address readers run over the text and over every decoded form of it, so `https%3A%2F%2F<host>%2F<ns>%2F<repo>` and `<user>&#64;<host>` are replaced or refused like their plain forms. Host-shaped is stated by construction, in one place (`closure.hosts`): a dotted name is a host **only when it is led or followed** — led by a scheme `://` or by `<user>@` (a run of two labels or more), or led by `//` or followed by `/<segment>` or `:<port>` (a run of at least three labels ending in an explicit TLD list: ISO 3166-1, `uk`, `eu`, `su` and the common generic and private-use suffixes, minus the fifteen country codes that are also file extensions, whose first label is not a reverse-DNS head, `com.example.app`); a dotted IPv4 quad follows the same rule, whatever its octets (a scheme, `//` or `<user>@` before it makes it a host whose namespace and repository are read; `:<port>` or `/<segment>` after it makes it an address that is judged but whose path is never read, so `curl 10.0.0.12:8080/metrics` is written as it stands; bare, it is text, so `JDK 1.8.0.252`, `pkg = 1.2.3.4` and `resolver 1.1.1.1` are never refused), a bracketed IPv6 literal may carry a `%<zone>` and a dotted IPv4 tail, and a dotted name that is neither led nor followed (`response.data.id`, `self.logger.info`, `Xcode.app`, `config.dev`, `ssl.ca`, `stream.io`, `os.path`, `file.py`, `README.md`) is ordinary text by construction, whatever its last label, and a host known from a structured field is replaced wherever it stands; a text path yields a namespace and a repository only when it holds both, names an owner through an API route, or stands before a GitLab `/-/` separator (`<group>/-/issues`, `groups/<g>/<sub>/-/epics`: everything before `/-/` is an identity, written `org/-/<route>`; a lone segment such as `/docs` or `/health` is a page and is never learnt), or sits in the identity slot of a route grammar read from one table (SP-RECORDER-REDACTS-BY-CLASS 0.22: GitLab's Web IDE `-/ide/project/<ns>/<repo>/edit/…`, Bitbucket Server's `users/<u>/repos/<r>` and `projects/<KEY>/repos/<r>`, GitHub's `orgs/<o>/teams/<t>`); a word a forge reserves for its own pages (`uploads`, `help`, `explore`, `admin`, `-`, `.well-known`, `marketplace`, `settings`, `account`, the table `RESERVED_ROUTE_WORDS`) is never an identity in a URL path and keeps that path as it stands, so `uploads/-/system/…` is not read as a namespace, and an identity after one (`s/<user>`, the admin area) is not read (NG4-14) — the recorded remote and an argv API path (`repos/<o>/<r>/…`, `projects/<ns>%2F<r>/…` as an argv string of a recorded call, and only there; which source names an identity, and how, is one ordered table, `PROVENANCE`, 0.26: in the structured sources of a recorded call — its argv, stdin, stderr, the field values of a JSON stdout and the method's arguments — `[/][api/v<N>/]repos/<o>/<r>/<word>` names a repository only when `<word>` is one of the 74 sub-resources of GitHub's REST repository endpoints (`pulls`, `issues`, `hooks`, `contents`…), so `repos/frontend/main.py` and a bare `repos/<a>/<b>` stay file paths; prose — a body, a merge request description, a title, a commit message, a review comment's `path`, a code block, a raw stdout, and every channel the call itself declares prose: an argv `body=`/`path=`/`title=` field, the argument of `--body`/`--title`/`--message`/`--description`, a stdin declared by `--body-file -` or `-F body=@-`, a positional argument the method's signature names `body` — is never read for one, prose being the channel and never a string's wording, and an API path cited there is written as it stands (NG4-16, the owner's structure/prose decisions); SP-RECORDER-REDACTS-BY-CLASS 0.25 to 0.28) are not URL paths: each names a repository by construction, the namespace every segment but the last, so a namespace that is a reserved word (`assets`, a real GitHub account) is still a known value, replaced everywhere (0.23, 0.24), and a nested namespace is a value as a whole path too, so it is written `org/project` wherever it stands (0.24); an npm tarball `<pkg>/-/<pkg>-<version>.tgz` on any host, scoped or under a Nexus prefix, names no one, so a yarn.lock line keeps `lodash`; an `@<scope>` read from a URL is never learnt, on any registry, GitLab's own (`api/v4/…/packages/npm/@<scope>/…`, where package forwarding brings `@types` and `@babel`) and a company's (`npm.<corp>/@<corp>/…`) included: a scope equal to a known namespace is replaced as that value, every other one is written as it stands (NG4-15, 0.24); and nothing is read after a placeholder-class or public host; the words a language defines after `@` (Python decorators, CSS at-rules, JSDoc, Javadoc, PHPDoc and Doxygen tags, the Java, Kotlin, Swift and Dart annotations) are code, never a mention, and a userinfo (`${TOKEN}@host`) blocks one; the oracle raises on no input (`[::ffff:01.2.3.4]`, a `U_` followed by thousands of digits, 20 000 nested `[`), its readers are linear in the text, and a recording nested past what the readers can walk is refused (`nesting too deep`, exit 2) rather than crashed on. An acceptance corpus of 904 rows (every verifier probe of rounds 5–12 and the rows generated across hosts, positions, carriers and eight languages) pins both the leaks that never survive and the ordinary text that is never refused or corrupted, and a class sweep of 6 475 cells (`tests/unit/test_forge_recorder_sweep.py`: every lead, token kind, follow, encoding and position of every rule, 46 realistic carriers, 40 ordinary lines, 31 forge route grammars, every forge's reserved words in a path and as a recorded remote's namespace, the npm tarball grammar on ten hosts, every language word as a login, nesting depth to 20 000; a deterministic fifth runs in the unit suite) holds every rule to the same goal. A third party's project linked on a public forge names no one of the recording and is not searched for as segments; a private host's path segments are. The threat model is **accidental leaks** from real
pull requests, not adversarial evasion: a login the recording never learns
cited only as a bare word (or kept in a user object with a `name` only, or under `LOGIN`, `user_login`, `author_login`), a bare or followed private IPv4 address, its path never read (`connect to host 10.0.0.5`, `curl 10.0.0.12:8080/metrics`),
a two-label apex host cited only un-led or protocol-relative and in no
structured field, a third party's private host cited bare in prose
(`Host build.acme.io is down`: no scheme, no `<user>@`, no `//` before it, no
`/<segment>` or `:<port>` after it; named test
`test_a_bare_dotted_host_in_prose_is_the_documented_residual`), a public IPv4
address cited bare, wherever it stands, or as a path segment of a URL (`resolver 104.16.0.1`, `ipinfo.io/104.16.0.1/json`: a version and an address cannot be told apart by their digits, so a bare quad is text and a quad after a single `/` is a version; named tests
`test_a_bare_dotted_quad_in_prose_is_ordinary_text_whatever_its_octets`, `test_a_quad_after_a_single_slash_is_a_version_and_a_comparator_allows_spaces`), a lone path segment after a host in a non-URL lead (`//code.kvncloud.io/kvnteam`, on the recorded instance too; named test `test_a_lone_path_segment_after_a_host_is_a_page_not_a_namespace`), a third party's namespace after a placeholder-class host (`http://localhost/<ns>/<repo>`; named test `test_nothing_is_read_after_a_placeholder_class_host`), the bare word of a mention-only handle shorter than four characters (`cc @bob, as bob said`; named test `test_a_short_mention_only_handle_is_replaced_in_its_at_form_and_never_refuses`), a framework's annotation in prose written as a class word (`@Autowired` → `@author-2`), a mention of a non-participant whose login is a language word written as it stands (`cc @test`; named test `test_a_mention_only_handle_equal_to_a_language_word_is_the_documented_residual`), an unbracketed IPv6 address and a bracketed literal with a path but no lead
(`[fd00::1]/ns/repo`; named test `test_a_bare_or_un_led_ipv6_address_is_the_documented_residual`), a project id of fewer than five digits in text, Unicode confusables and invisible characters, Markdown-split or
escaped values, non-node-id base64 and JSON-in-a-string escapes are out of
scope (SP-RECORDER-REDACTS-BY-CLASS Threat model & NG4).
A `<user>@` lead is a local part of ASCII mailbox characters or any Unicode
letter or digit (`josé@<host>`), or a URL's userinfo from `<scheme>://` to the
`@` whatever it ends with (`https://u:p=@<host>`); a fixture is refused when
any string it writes, the method's arguments and argv included, carries a
token-shaped value, or a home path in any decoded form (`/home/<x>`,
`/Users/<x>`, `/var/home/<x>`, `C:\Users\<x>`, `C:/Users/<x>`, and the Windows
profile as a POSIX layer mounts it: WSL `/mnt/<drive>/Users/<x>`, Git-Bash
`/<drive>/Users/<x>`, Cygwin `/cygdrive/<drive>/Users/<x>`), live or offline; a host
is also a one-label authority after `<scheme>://` (`https://gitlab/…`) or a name
whose last label is an IDNA `xn--` label. Every decoded form is read in NFC and
NFKC as well and with its Unicode labels in IDNA A-labels, so a host or a mailbox
on an internationalised domain (`<x>@müller.de`, `https://éclair.fr/…`, a
fullwidth URL, an ideographic full stop) is read by the same grammar as an ASCII
one and refused, while non-ASCII prose keeps no host it did not have. The first
path segment of a container registry is an account, as `github.com/<owner>` is
(`ACCOUNT_NAMESPACES`: `ghcr.io/<owner>`, `docker.io/<namespace>` after an OCI `v2/`,
npm's `~<user>`; `docker.io/library`, the official images, holds none), and so is
the segment after an account route of a listed registry (`TENANT_ROUTES`: PyPI
`user`/`org`, crates.io `users`/`teams`, NuGet `profiles`, npm `org`/`settings`,
jsDelivr `gh`): both are refused as a `tenant path` whatever leads the host, and a
URL on one is rewritten to the placeholder. The deny-list reader and the last judge before a write
live in `repoach.forge.denylist` and `repoach.forge.fixture_judge`, shared by
the live recorder and the offline transform, as is the one role builder,
`repoach.forge.recording.assign_roles`. The deny-list fails closed: an entry point
called without one reads the maintainer's, and only an explicit empty
`Denylist()` is empty. A deny-list file that exists but cannot be read is a refusal naming the reason, never an
empty list, and no caller is handed an entry. The token shapes are judged on every decoded
form of a string, as the home path and the deny-list are. The recorder's own `git` child (`origin_url`) runs with
`PATH`, `HOME`, `XDG_CONFIG_HOME`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`,
`GIT_CONFIG_NOSYSTEM` and `SYSTEMROOT` only (`GIT_ENV_KEYS`), never a token or the deny-list.

Public attribution is not personal data: the copyright holder in `LICENSE`,
the `authors` of `pyproject.toml`, the security contact in `SECURITY.md`, the
maintainer column of `docs/languages.md` and a spec's `author:` line stay.
