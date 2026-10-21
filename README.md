<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
  <img src="assets/logo-light.svg" alt="Repoach — anvil and spark" width="110">
</picture>

# Repoach

[![CI](https://github.com/repoachhq/repoach/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/repoachhq/repoach/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE) [![Built by Repoach](https://img.shields.io/badge/built%20by-Repoach-orange)](https://github.com/repoachhq/repoach)

</div>

---

Evidence-first merge gate and PR reviewer for GitHub and GitLab. Bring your Claude Code login or your key. Install in minutes.

**How it decides.** Four reviewers (Architect, Sentinel, Tester, Scribe) read the pull or merge request and write their findings to a ledger. Mechanical verifiers check what can be checked on disk, and an adversarial refuter on a heavier model must fail to disprove a judged finding before it counts. The merge gate then re-verifies that ledger at the exact head it is about to merge (CI green, zero surviving blockers, a complete review, the spec's promised tests present) and refuses an LGTM it cannot prove. repoach is made by one maintainer plus agents, and is open to contributors.

## Quickstart

repoach is not on PyPI yet (the `repoach` name there is a reservation; the release is `SP-PYPI-RELEASE`). Until then, install from the repository:

```bash
pipx install git+https://github.com/repoachhq/repoach@main
cd your-repo
repoach init                 # writes repoach.toml and the spec template
repoach doctor               # which provider route is configured
```

See it before installing: [repoach-demo](https://github.com/repoachhq/repoach-demo) replays a recorded review and the gate refusing a pull request with three planted defects (`make demo`, or open it in Codespaces or Gitpod).

Until the next release, `main` lags `develop`; to get the provider routes and `init --ci` today: `pipx install git+https://github.com/repoachhq/repoach@develop`.

Pick a provider (below), log in to your forge (`gh auth login` or `glab auth login`), then:

```bash
repoach review pr <N> --dry-run   # run the review bench on a PR or MR, post nothing
repoach review gate <N>           # the evidence-first merge gate, read-only
```

`repoach init` ran in 7 s on the `gum` repository (measured 2026-10-19/21). The **[getting-started guide](docs/getting_started.md)** covers the rest of the CLI.

### Providers

Every model call goes through one provider, chosen by `--provider` (on `review pr`, `review gate`, `ci review`), else `REPOACH_PROVIDER`, else `[provider].kind` in `repoach.toml`. You bring your own key or login; repoach sells no tokens. Keys live in the environment or `~/.config/repoach/env`, never in a project file.

| Kind | Configure | Where the key goes | Models | Cost shown |
|---|---|---|---|---|
| `claude_code` | `kind = "claude_code"` and a logged-in `claude` CLI | none: the CLI's login (`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` on a runner) | `opus` / `sonnet` / `haiku` aliases | tokens only, `subscription (tokens only)` |
| `anthropic` | `kind = "anthropic"`; `REPOACH_ANTHROPIC_BASE_URL` for a proxy (with or without `/v1`) | `REPOACH_ANTHROPIC_API_KEY`, else `ANTHROPIC_API_KEY` | `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5` | priced |
| `openai_compatible` | `kind = "openai_compatible"` and `base_url` (or `REPOACH_OPENAI_BASE_URL`; `http://host:port` gets `/v1`) | `REPOACH_OPENAI_API_KEY`, else `OPENAI_API_KEY`; optional for a local server | required per tier | unpriced unless the model is in the table |
| preset `openai` | `kind = "openai"` | `REPOACH_OPENAI_API_KEY`, else `OPENAI_API_KEY` | required per tier | priced for the listed models |
| preset `ollama` | `kind = "ollama"` (`http://localhost:11434/v1`) | none | required per tier (e.g. `llama3.1:8b`) | `$0` (self-hosted) |
| preset `openrouter` | `kind = "openrouter"` | `REPOACH_OPENROUTER_API_KEY`, else `OPENROUTER_API_KEY` | required per tier (e.g. `google/gemini-3.8-flash`) | unpriced |
| presets `groq`, `deepseek`, `mistral` | `kind = "groq"` … | `REPOACH_<NAME>_API_KEY`, else `<NAME>_API_KEY` | required per tier | unpriced |
| `gemini` | `kind = "gemini"` (Google's Generative Language API, OpenAI-compatible root `/v1beta/openai`) | `REPOACH_GEMINI_API_KEY`, else `GEMINI_API_KEY`, else `GOOGLE_API_KEY` | required per tier (e.g. `gemini-3.8-flash`) | priced for the listed models |
| `gateway` | `kind = "gateway"` (or nothing named) and `REPOACH_LLM_PROXY_BASE_URL` | `REPOACH_ANTHROPIC_AUTH_TOKEN` (the gateway's secret; upstream keys stay in the gateway) | the gateway's chains | unpriced |

Every kind but `gateway` runs the gateway's own transport inside the review process: no gateway, no port. `base_url` / `REPOACH_OPENAI_BASE_URL` applies to `openai_compatible` alone: a preset or `gemini` fixes its host and refuses an override. Gemini through OpenRouter is the `openrouter` preset with a `google/…` model id.

```toml
# repoach.toml
[provider]
kind = "ollama"
opus = "llama3.1:8b"
sonnet = "llama3.1:8b"
haiku = "llama3.1:8b"
```

```bash
repoach doctor                    # provider_route=direct (ollama), one provider_option= line per route
repoach review pr <N> --dry-run   # JSON with "usage"; stderr: repoach: usage: 8 calls, … tokens, … s, <cost>
repoach review pr <N> --dry-run --provider claude_code
repoach usage --since 7d --by reviewer
```

- **Models.** Each agent asks for a tier (`opus`, `sonnet`, `haiku`). Set `[provider].opus` / `.sonnet` / `.haiku` or `REPOACH_MODEL_OPUS` / `_SONNET` / `_HAIKU`; only `claude_code` and `anthropic` have defaults.
- **Usage and cost.** Every call writes one row (reviewer, provider, model, tokens, duration, cost estimate or null) to the `usage_ledger` table of the review database. Prices come from a dated table (`src/repoach/providers/prices.py`, read from the vendors' pricing pages on 2026-10-21, each row with its URL), keyed on the API host the call reached and the model; a call to any other host (a proxy, a compatible server) or a model absent from it is counted as unpriced, never guessed, and a subscription shows tokens only. `repoach usage --since 7d|24h|2026-10-01 [--by model|provider|reviewer|pr|day|command] [--json]` sums any period.
- **Claude Code details.** The CLI is `[provider].cli_path` or `REPOACH_CLAUDE_CODE_CLI_PATH`, else `claude` on `PATH`. Each call runs in an empty temporary directory (your project's `CLAUDE.md` is never loaded), in its own process group, killed with its children on timeout. The CLI is spawned without any `*_API_KEY`, `*_AUTH_TOKEN` or `*_TOKEN` variable of yours, so it answers on the login and never bills a key it would otherwise find; the one token it keeps is `CLAUDE_CODE_OAUTH_TOKEN`.
- **Never implicit.** `claude_code` is used only when named. Nothing named and no gateway URL is one error, rc 2, before any forge call: `repoach: no provider configured -- pass --provider, or set REPOACH_PROVIDER or [provider].kind in repoach.toml, to a kind (claude_code, anthropic, openai_compatible, gemini, gateway) or a preset (openai, ollama, openrouter, groq, deepseek, mistral)`.
- **Failing early.** A missing CLI, a logged-out CLI, a missing key, model or base URL is one line on stderr and exit code 2, before any forge call; a key the provider rejects (401/403) stops the reviewer at the first attempt with the variable's name, never its value.

## Works after your agents, not instead of them

repoach does not write your code. Aider, OpenHands, the Copilot coding agent, Cursor and the humans on your team are PR sources: they open the pull or merge request, and repoach reviews it and gates the merge. It sits where a CI barrier sits, after whatever produced the change, and it judges an agent's PR and a person's PR the same way.

## What the gate refuses

Two excerpts from recorded runs on this repository's public PRs.

**A finding refuted before it was believed.** On [PR #217](https://github.com/repoachhq/repoach/pull/217), the Scribe flagged `src/repoach/ci_entry/advisory.py:672`: "`apply_forge_env` mutates `os.environ` via `setdefault`, which the module docstring does not mention." The refuter answered on the [same thread](https://github.com/repoachhq/repoach/pull/217#discussion_r4327345489):

> apply_forge_env is explicitly named and documented as exporting env for the PR-state read, and it only uses setdefault. [...] this is a style preference, not a defect.
>
> this `design` finding was refuted by the `refuter` check and marked ``refuted`` in the findings ledger

**An LGTM refused.** On [PR #202](https://github.com/repoachhq/repoach/pull/202), the bench's `final_verdict` was `APPROVE`, with zero blockers and green CI. The [review report](https://github.com/repoachhq/repoach/pull/202#issuecomment-6266037175) still reads:

```markdown
## Decision: BLOCKED at `0c69899a7c6d` — spec acceptance selectors not all present

| Fact | Value |
| --- | --- |
| CI green | True |
| Open blocking findings | 0 |
| Spec covered | False |
| Review complete | True |
```

The approval was not enough: the tests the plan promised were not present at that head, so the gate said no.

<details>
<summary><b>The review pipeline</b>: findings, mechanical verifiers, adversarial refuter, and the merge gate</summary>

![Repoach — evidence-first review pipeline](docs/review_redesign_architecture.png)
</details>

## How it compares

| Product | Forges | Blocks a merge on evidence | Re-verifies findings at the merge head | Spec in the repo gates the plan | Model choice (BYOK) | Self-host | Price as listed |
|---|---|---|---|---|---|---|---|
| repoach | GitHub, GitLab | yes: refuses an unproven LGTM | yes: the ledger is re-verified at the exact head | yes: governed spec; the plan's tests must exist at the head | Claude Code login, Anthropic key, OpenAI-compatible presets, Gemini, gateway | yes: your CI or your machine; no repoach cloud | free (MIT); you pay your model, no token resale |
| GitHub Copilot code review | GitHub; Azure DevOps (preview) | no: can approve, when enabled, toward required approvals | n/m | n/m | no: model switching not supported | n/m | $0.05–5 of AI credits per review |
| Cursor Bugbot | GitHub, GitLab, Bitbucket, Azure DevOps | no: neutral status unless fail-on-unresolved is enabled | n/m | n/m | Cursor's model setting; no key of yours named | no: managed service; self-hosted forges connect to it | $1.00–1.50 per average review |
| CodeRabbit | GitHub, GitLab, Azure DevOps, Bitbucket | n/m | n/m | n/m | n/m | Enterprise plan | $24–72 per user per month (annual) |
| Qodo PR-Agent | GitHub, GitLab, Bitbucket, Azure DevOps, Gitea | no: comments only | n/m | n/m | yes: any model, your key | yes: GitHub Action, CLI, Docker | free (MIT) |
| Greptile | GitHub, GitLab, Bitbucket, Gitea, Perforce | n/m | n/m | n/m | n/m | Enterprise plan (Docker, Helm, air-gapped) | $30 per seat per month |
| Sourcery | GitHub, GitLab (incl. self-managed) | n/m | n/m | n/m | your LLM keys on Enterprise | Enterprise plan | $12–30 per developer per month; free for open source |
| GitLab Duo Code Review | GitLab | no: feedback only | n/m | n/m | GitLab's models or self-hosted models | GitLab Self-Managed, Dedicated | $0.25 per review, flat (Code Review Flow) |
| Claude Code Review | GitHub | no: never approves or blocks | no: verifies before posting, not at merge | n/m | no: the service chooses the models | no: vendor infrastructure | token usage, no flat per-review price |

Each cell is copied from [docs/comparison.md](docs/comparison.md), which links the page every value was read on; `n/m` means the page says nothing, so this table cannot say. All cells checked on 2026-10-21.

### What repoach does not do

- **No cloud.** It runs in your CI or on your machine; there is no hosted repoach to sign up to.
- **No token resale.** You bring a login or a key and pay your model provider directly.
- **No mathematical proof.** The gate re-verifies each finding's claim against the code at the head and requires green tests; it proves no property of your program.
- **It is as good as your tests.** "CI green" and "the spec's tests are present" mean what your test suite means; a weak suite makes a weak gate.
- **No precision number yet.** `repoach bench` measures it on a labelled corpus ([Precision](#precision)), but no finding carries a human label yet, so precision and recall read `cannot say`.

## Run repoach in CI

`repoach ci review` reviews the pull / merge request of the CI job it runs in — no shell driver per project. It reads the forge and the PR number from the CI (`CI_MERGE_REQUEST_IID` on GitLab CI, `GITHUB_EVENT_PATH` on GitHub Actions; `--pr N` and `--forge` override), prints one JSON document on stdout and one human line on stderr, and is **advisory**: it always exits 0 unless `--strict`. A missing input is a named skip (`skipped, GITLAB_TOKEN / REPOACH_GITLAB_TOKEN is not set`), and nothing is posted unless `REPOACH_ADVISORY_POST` is exactly `1`. `repoach doctor --ci` lists, by name only, the variables it reads and which are present.

Write the job, then commit it:

```bash
repoach init --ci gitlab   # writes .repoach/ci/gitlab-review.yml, prints the pin and the include
repoach init --ci github   # writes .github/workflows/repoach-review.yml, prints the pin
repoach init --ci gitlab --print   # print only, write nothing
```

The job pins the repoach that ran `init`, never a branch: the commit pip installed it from, the `HEAD` of a repoach checkout (push it before the job runs), or `repoach==<version>` from PyPI; `--ref <tag-or-sha>` or `--pypi-version X.Y.Z` override it.

On GitLab, add the printed include to `.gitlab-ci.yml` (repoach never edits it); override any key of the `review:advisory` job beside it, for example the runner tag:

```yaml
include:
  - local: .repoach/ci/gitlab-review.yml
review:advisory:
  tags: [my-runner]
```

The job installs the pinned repoach with every secret unset for the install step, then runs `repoach ci review`. Set, as masked CI/CD variables (GitLab) or secrets (GitHub):

| | GitLab | GitHub |
|---|---|---|
| Forge token | `REPOACH_GITLAB_TOKEN` (project access token, `api`) | `GITHUB_TOKEN` (provided) |
| API key | `REPOACH_PROVIDER=anthropic` + `REPOACH_ANTHROPIC_API_KEY` | variable `REPOACH_PROVIDER=anthropic` + secret `REPOACH_ANTHROPIC_API_KEY` |
| Claude Code | `REPOACH_PROVIDER=claude_code` + `CLAUDE_CODE_OAUTH_TOKEN`, `claude` installed in the job | the same |
| Other providers | `REPOACH_PROVIDER=<kind or preset>` + its key and `REPOACH_MODEL_*` ([Providers](#providers)) | the same |
| Gateway mode | `REPOACH_LLM_PROXY_BASE_URL` + `REPOACH_ANTHROPIC_AUTH_TOKEN` | variable `REPOACH_LLM_PROXY_BASE_URL` + secret `REPOACH_ANTHROPIC_AUTH_TOKEN` |
| Posting | `REPOACH_ADVISORY_POST=1` | variable `REPOACH_ADVISORY_POST=1` |

With **any named provider but `gateway`** (`REPOACH_PROVIDER`, `--provider` or `[provider].kind`), the review calls the model itself: no gateway runs, and in `anthropic` mode the key reaches the review. In **gateway mode** the review calls a repoach gateway you already run. A job that sets only `REPOACH_ANTHROPIC_API_KEY` and names no provider keeps the older **direct-provider mode**: it starts a gateway on `127.0.0.1` for its own duration, with the Anthropic key as its only upstream (`REPOACH_MODEL_OPUS` / `_SONNET` / `_HAIKU` override the models) and a per-job shared secret; the key reaches that gateway and never the review, and the forge token reaches the review and never the gateway. Posting on GitLab also needs the bot's username (`[review].bot_username` in `repoach.toml`, or `REPOACH_REVIEW_BOT_LOGIN`).

Exit codes with `--strict`: `0` reviewed (APPROVE/COMMENT) or the PR already merged/closed, `1` internal error, `2` a missing input, bad flag or config, forge or provider unavailable, `3` a reviewer reached no model or the review outran `--timeout` (minutes, default 25; the child is killed with its process group), `4` changes requested. Any exception is one `internal_error` JSON, never a traceback.

## Safety

Letting agents review and merge code is only acceptable if the blast radius is bounded. In repoach:

- Agents **never merge to the release branch** (`main`): the release gate is read-only and the operator merges. Into the integration branch (`develop`) they merge only through the evidence-first gate, and only when CI is green at the exact head.
- Every agent fix is **path-whitelisted** — no touching `.github/`, `.githooks/`, `prompts/review/`, `.git/` or any `.env*` file.
- Secrets are **scrubbed** from every subprocess that executes agent-authored code (test reruns, the Developer's tools, the read-only `claude -p` exploration): any variable named like a token, key, secret, password or credential is dropped.
- The chain autopilot is **shadow by default**, capped at two chain mutations per cycle, and writes `chains.env` atomically with a `.bak` only when writing is enabled with `--apply` or `REPOACH_CHAINPILOT_APPLY_ENABLED=true`.

## Status

Early, and built in the open. Every row is measured, not promised, and names what it was read on.

| Area | Today | Read on |
|---|---|---|
| Forges | GitHub (`gh`) and GitLab (`glab`) behind one `Forge` protocol (`src/repoach/forge/`). | `develop` 45712b2, 2026-10-21 |
| Languages and gates | Lint, test and gate commands and per-language verifiers (Python, the TypeScript family, YAML, Markdown links, external tools such as `verilator`) from `repoach.toml` (`src/repoach/gates/`); a check that cannot run answers `not_applicable` with the reason. | `develop` 45712b2, 2026-10-21 |
| Install | `pipx install git+https://github.com/repoachhq/repoach@main`; the `repoach` on PyPI is a name reservation until `SP-PYPI-RELEASE`. `repoach init` ran in 7 s on the `gum` repository, and `init --ci gitlab\|github` writes the job. | owner runs, 2026-10-19/21 |
| Providers | Claude Code login, Anthropic key, OpenAI-compatible presets (OpenAI, Ollama, OpenRouter, Groq, DeepSeek, Mistral), Gemini, and the gateway ([Providers](#providers)). | `develop` 45712b2, 2026-10-21 |
| First external client | Yoob (GitLab; Python, TypeScript and Verilog). On its first runs the gate flagged 3 majors on the secrets scoping of Yoob's own CI wiring of repoach. | owner runs, 2026-10-19/21 |
| Time and tokens | A real review of a Yoob merge request, run from the client repository through a Claude Code login: 69–115 s, about 224–300 k tokens per review, more than 95 % of them cached. | owner runs, 2026-10-19/21 |
| Precision | Measured on the planted-defect corpus with `claude_code`; precision and recall wait on human labels. The table, its date and corpus size are generated, never typed: [Precision](#precision). | `repoach bench`, 2026-10-21 |
| Known review defect | A dry run has no head SHA: the head guard starts only when posting (`if self._post:` in `src/repoach/review/orchestrator.py`), so its ledger rows carry `head_sha = NULL`. The other 2026-10-18 defect, a dry run posting refuted-finding replies, is closed: the sentinels are skipped in a dry run. | `develop` 45712b2, 2026-10-21 |
| Activity | Last merge #220 on 2026-10-21. | GitHub PR list, 2026-10-21 |

## Precision

A reviewer is worth what its findings are worth. `repoach bench run` reviews every case of a labelled corpus through the real pipeline (a dry run on a throw-away ledger), `repoach bench report` scores the run against the human labels, and the block below is written by `repoach bench report <result> --update-readme README.md`, never by hand. The published one-line number (`--readme-line`) is refused until the corpus holds 100 labelled findings. Corpus format, labelling and the scoring rules: [bench/corpus/README.md](bench/corpus/README.md).

<!-- bench:precision:start -->
corpus planted@813105285fc7, provider claude_code, measured 2026-10-21, 10 cases scored, 0 cases skipped, 0 labelled findings

| reviewer | TP | FP | unsure | precision | refuter precision | recall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| architect | 0 | 0 | 0 | cannot say | cannot say | cannot say |
| sentinel | 0 | 0 | 0 | cannot say | cannot say | cannot say |
| tester | 0 | 0 | 0 | cannot say | cannot say | cannot say |
| scribe | 0 | 0 | 0 | cannot say | cannot say | cannot say |

| severity | TP | FP | unsure | precision | refuter precision | recall |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| blocker | 0 | 0 | 0 | cannot say | cannot say | cannot say |
| major | 0 | 0 | 0 | cannot say | cannot say | cannot say |
| minor | 0 | 0 | 0 | cannot say | cannot say | cannot say |
| nit | 0 | 0 | 0 | cannot say | cannot say | cannot say |

precision cannot say, refuter precision cannot say, recall cannot say, unsure 0, unlabelled 45
planted defects located: 10/10 (a non-refuted finding overlaps the defect); recall waits on labels for 10

| case | findings | refuted | planted | located | tokens | wall s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 01-security-sql-injection | 5 | 2 | 1 | 1 | 262930 | 140 |
| 02-security-xss-innerhtml | 4 | 0 | 1 | 1 | 239389 | 124 |
| 03-logic-discount-inverted | 3 | 1 | 1 | 1 | 243115 | 87 |
| 04-logic-total-ignores-quantity | 2 | 0 | 1 | 1 | 189792 | 66 |
| 05-test-gap-bulk-price | 3 | 2 | 1 | 1 | 160341 | 64 |
| 06-test-gap-remove-line | 4 | 1 | 1 | 1 | 212337 | 110 |
| 07-docstring-missing | 2 | 0 | 1 | 1 | 134989 | 75 |
| 08-docstring-stale-args | 10 | 5 | 1 | 1 | 349091 | 160 |
| 09-convention-bare-except | 6 | 1 | 1 | 1 | 296644 | 149 |
| 10-convention-any-and-loose-equality | 6 | 2 | 1 | 1 | 294487 | 148 |
<!-- bench:precision:end -->

`repoach digest --since 7d` reads the same ledger the other way: the findings the refuter refuted, the merges the gate refused, and the merges that passed after a refusal, each with its PR link.

## Later: the spec-driven builder

repoach also builds. The same governed specs that gate a review can drive a Planner, a Developer and a Coder that write the change themselves; this is how repoach builds repoach, and it comes after the gate in this README because the gate is what you install first.

### Governed specs

Every change is anchored to a spec whose frontmatter declares **what it owns** and **what it depends on**. From that, repoach *derives* the system's dependency graph and *enforces* it: an import that crosses a boundary the spec never declared fails CI (`repoach arch check`). The architecture cannot silently drift from the docs.

### Failover routing, with a shadow autopilot

In gateway mode, repoach runs its agents through one gateway with ten provider slots (NVIDIA NIM, OpenRouter, Kimi, Groq, Cerebras, DeepSeek, OpenAI, Anthropic, any OpenAI-compatible endpoint, and the local `claude` CLI) and walks a failover chain per capability tier (`chains.env`). A chain autopilot watches each head's live health and plans rewrites of the chains, bounded by a hard cap of two mutations per cycle. It runs in **shadow mode** unless writing is enabled with `--apply` or `REPOACH_CHAINPILOT_APPLY_ENABLED=true`: it journals what it would change and writes nothing.

### It builds itself

Every substantive change to repoach starts as a spec in `docs/specs/`, is planned and implemented by its own Developer, and is opened as a PR against `develop` that the four-reviewer bench reviews. Of the 200 PRs merged by 2026-10-18, 65 were merged by the bot lane and 135 by the maintainer through `scripts/safe_merge.sh`, which runs the same review and gate before `gh pr merge` (measured 2026-10-18 on the GitHub PR list).

![Repoach — the review factory, from spec to merge](docs/review_factory_architecture.png)

<details>
<summary><b>Builder memory</b>: past lessons recalled before planning, each build's outcome remembered</summary>

![Repoach — builder memory loop](docs/builder_memory_architecture.png)
</details>

<details>
<summary><b>Provider observability</b>: active probes and passive telemetry around the LLM gateway</summary>

![Repoach — NIM observability](docs/nim_observability_architecture.png)
</details>

### Watch it work

A spec going from *written* to *shipped*. The values are illustrative and each JSON is abridged to the fields that matter; `repoach <command> --help` lists the exit codes.

**1 · You write a spec, the Developer builds it.** The Planner drafts a plan (`docs/plans/<SP-ID>.md`), then the Developer executes it one commit per step and opens a PR against `develop`:

```console
$ repoach develop SP-BUDGET-RETRY-BACKOFF
{
  "spec_id": "SP-BUDGET-RETRY-BACKOFF",
  "branch": "feat/sp-budget-retry-backoff-impl",
  "steps_total": 4,
  "steps_completed": 4,
  "ruff_passed": true,
  "pytest_passed": true,
  "self_verified": true,
  "pushed": true
}
```

**2 · Four reviewers judge it, independently.** A single `REQUEST_CHANGES` wins:

```console
$ repoach review pr 42
{
  "pr_number": 42,
  "final_verdict": "REQUEST_CHANGES",
  "n_blockers": 1,
  "reviews": [
    { "role": "architect", "verdict": "APPROVE",         "summary": "Boundary clean; policy owned by the spec." },
    { "role": "sentinel",  "verdict": "REQUEST_CHANGES", "summary": "Backoff has no jitter cap — a retry storm is reachable." },
    { "role": "tester",    "verdict": "APPROVE",         "summary": "Integration test exercises breaker recovery." },
    { "role": "scribe",    "verdict": "APPROVE",         "summary": "Docstrings Google-style; spec cross-referenced." }
  ]
}
```

**3 · The Coder fixes what they found, then they re-verify.** One findings-driven iteration; each resolved finding is re-checked at the new head:

```console
$ repoach review fix 42
{ "n_open_findings": 1, "fixes_applied": 1, "pytest_passed": true, "pushed": true, "resolved": 1, "still_open": 0 }
```

**4 · The gate re-checks the facts at the head, and only then merges.**

```console
$ repoach review gate 42
{
  "pr_number": 42,
  "head_sha": "9f6cd85",
  "merge": true,
  "reasons": [],
  "facts": { "ci_green": true, "open_blocking_findings": 0,
             "spec_covered": true, "review_complete": true }
}
```

`merge: true` → `repoach review merge 42` squash-merges the PR into `develop`. `merge: false` prints its reasons and blocks. The read-only gate and the merge share a single code path, so what you inspect is exactly what ships.

## Contributing

Issues, docs, spec proposals, and code PRs are all welcome, from people and from their agents. Every PR by an allow-listed actor goes through the same bot-review team and evidence-first merge gate; the workflows run on a self-hosted runner behind an actor allowlist, so a PR from outside is picked up by the maintainer and run through the same pipeline by hand. Start with **[CONTRIBUTING.md](CONTRIBUTING.md)**: it explains the spec-driven workflow, the quality gates, and what to expect from the review bots. New to the codebase? Pick an open [`good first spec`](https://github.com/repoachhq/repoach/issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+spec%22) issue.

---

<div align="center"><sub>MIT · <a href="https://github.com/repoachhq/repoach">github.com/repoachhq/repoach</a></sub></div>
