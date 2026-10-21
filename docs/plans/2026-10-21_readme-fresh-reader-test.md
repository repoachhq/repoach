# README fresh-reader test (SP-README-PRODUCT-STATEMENT AC6)

Run 2026-10-21 on the README of branch `docs/comparison-page-and-readme-statement` (base `develop` 45712b2).

- **Model:** `claude-sonnet-5-5`, through `claude -p --model sonnet --tools "" --setting-sources ""`, run from an empty directory: no tools, no settings, no repository access.
- **Input:** the first 40 lines of the stripped README, `sed -e '/^<div/,/^<\/div>/d' -e '/^$/d' -e '/^---$/d' README.md | head -40`, appended to the question below.
- **Pass condition:** at least two of the three products named are in the F3 list of the spec (GitHub Copilot code review, Cursor Bugbot, CodeRabbit, Qodo PR-Agent, Greptile, Ellipsis, Sourcery, GitLab Duo Code Review).

## Question

```text
Below is the top of a README for a software project. You have no other information about it and no tools. Question: which three existing products is this closest to? Answer with three product names, one per line, each with a one-line reason.
```

## Answer, verbatim

```text
CodeRabbit: an AI pull request reviewer for GitHub and GitLab that posts findings on PRs/MRs.
Qodo Merge (formerly PR-Agent): an open-source, bring-your-own-model PR reviewer with multi-provider support and a CLI/CI mode.
Greptile: an AI code reviewer that reads the whole repo and flags issues in PRs, with a focus on review quality and catching real bugs.
```

## Verdict

Pass: 3 of 3 are in the F3 list (CodeRabbit, Qodo PR-Agent under its hosted name Qodo Merge, Greptile). None of the autonomous developers or forges the 2026-10-20 reading named (OpenHands, SWE-agent, Aider, Forgejo) appears.

<details>
<summary>The input given to the model</summary>

````markdown
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
````
</details>
