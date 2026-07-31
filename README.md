<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
  <img src="assets/logo-light.svg" alt="Repoach — anvil and spark" width="110">
</picture>

# Repoach

### The self-forging software factory.

*Plug it into your repo — it builds your system, and keeps forging itself.*

[![CI](https://github.com/repoachhq/repoach/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/repoachhq/repoach/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE) [![Built by Repoach](https://img.shields.io/badge/built%20by-Repoach-orange)]()

</div>

---

**Repoach** is an autonomous software factory. You point it at a repository and it ships changes through a multi-agent review pipeline that verifies its own work before anything merges. It runs its agent fleet over a self-healing, multi-provider LLM gateway — and it improves its own infrastructure as it goes.

> *ferro* — iron. The metal you forge, and re-forge.

## Why Repoach is different

Most "AI writes your code" tools trust the model. **Repoach trusts evidence.**

### 1. Evidence-first merge gate
Agents can't approve their own work. Every finding a reviewer raises is **re-verified against the current HEAD** before a merge is allowed. There is no forgeable "LGTM" — the gate re-checks the facts (CI green, zero surviving blockers, complete review) at merge time, every time.

### 2. Governed specs
Every change is anchored to a spec whose frontmatter declares **what it owns** and **what it depends on**. From that, Repoach *derives* the system's dependency graph — and *enforces* it: an import that crosses a boundary the spec never declared fails CI. The architecture can't silently drift from the docs.

### 3. Self-evolving routing
Repoach runs its agents across a dozen LLM providers with automatic failover. A built-in autopilot watches each provider's live health and **rewrites its own routing** to stay fast and cheap — bounded by hard safety caps.

### 🔁 It builds itself
Repoach is built by Repoach. Every PR in this repo went through Repoach's own review pipeline and merge gate. **The factory is its own first customer.**

## How it works

A spec's life, top to bottom: build → review → ship. Four reviewers and a claim-verification layer feed a findings ledger; a pure evidence-first gate re-verifies everything at the exact head it is about to merge.

![Repoach — the review factory, from spec to merge](docs/review_factory_architecture.png)

### Deeper dives

<details>
<summary><b>The review pipeline</b> — findings, mechanical verifiers, adversarial refuter, and the merge gate</summary>

![Repoach — evidence-first review pipeline](docs/review_redesign_architecture.png)
</details>

<details>
<summary><b>Builder memory</b> — the factory recalls past lessons before planning and remembers each build's outcome</summary>

![Repoach — builder memory loop](docs/builder_memory_architecture.png)
</details>

<details>
<summary><b>Provider observability</b> — active probes + passive telemetry around the self-healing LLM gateway</summary>

![Repoach — NIM observability](docs/nim_observability_architecture.png)
</details>

## Watch it work

Here's a spec going from *written* to *shipped* — the same loop every change in this repo travels. The values below are illustrative; the command shapes are exactly what the CLI emits.

**1 · You write a spec, the factory builds it.** The Planner drafts a plan, then executes it one commit per step and opens a PR against `develop`:

```console
$ repoach develop SP-BUDGET-RETRY-BACKOFF
planner   ▸ 4 steps · plan docs/plans/SP-BUDGET-RETRY-BACKOFF.md
step 1/4  ▸ add backoff schedule to RetryPolicy            ✓ committed
step 2/4  ▸ thread policy through the proxy call site      ✓ committed
step 3/4  ▸ unit tests for the backoff schedule            ✓ committed
step 4/4  ▸ integration test: breaker recovers under load  ✓ committed
pushed feat/sp-budget-retry-backoff-impl · opened PR #42
```

**2 · Four reviewers judge it — independently.** Architect, Sentinel, Tester, and Scribe each return a verdict. A single `REQUEST_CHANGES` wins:

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

**3 · The Coder fixes what they found — then they re-verify.** One findings-driven iteration; each resolved finding is re-checked at the new head:

```console
$ repoach review fix 42
{ "n_open_findings": 1, "n_resolved": 1, "pytest": "green", "pushed": true }
```

**4 · The gate re-checks the facts at HEAD — and only then merges.** No "LGTM" is trusted: the gate re-verifies the findings ledger against the exact commit it is about to merge:

```console
$ repoach review gate 42
{
  "pr_number": 42,
  "head_sha": "9f6cd85",
  "merge": true,
  "reasons": ["CI green", "0 blocking findings survive re-verification",
              "review complete", "spec covered"],
  "facts": { "ci_green": true, "open_blocking_findings": 0,
             "spec_covered": true, "review_complete": true }
}
```

`merge: true` → the PR squash-merges into `develop`. `merge: false` prints its reasons and blocks. That decision is byte-identical to the one `repoach review merge` acts on — the read-only gate and the merge share a single code path, so what you inspect is exactly what ships.

## Quickstart

```bash
git clone https://github.com/repoachhq/repoach && cd repoach
pip install -e ".[dev]"

repoach --help            # the CLI
repoach review pr <N>     # run the review team on a pull request
repoach review gate <N>   # evidence-first merge gate, read-only
repoach develop <SP-ID>   # plan-driven build from a spec
```

New here? The **[getting-started guide](docs/getting_started.md)** walks
you from clone to a live, authenticated call through the LLM gateway —
provider keys, `chains.env` routing, the CLI tour, and wiring the
review factory to your own repo.

## Safety

Letting agents write and merge code is only acceptable if the blast radius is bounded. In Repoach:

- Agents **never merge to your protected branches** — they open PRs and stop.
- Every agent fix is **path-whitelisted** — no touching CI config, workflows, or secrets.
- Secrets are **scrubbed** from every agent's environment.
- The self-evolving router is bounded by **per-cycle caps** and **can't deploy without a read**.

## Contributing

Issues, docs, spec proposals, and code PRs are all welcome — but this
repo works a little differently: the factory is its own primary
developer, and every PR (bot or human) goes through the same
bot-review team and evidence-first merge gate. Start with
**[CONTRIBUTING.md](CONTRIBUTING.md)** — it explains the spec-driven
workflow, the quality gates, and what to expect from the review bots.

## Status

Early, and built in the open. If the idea resonates — an autonomous factory that's *transparent, self-evolving, and safe to be wrong* — ⭐ the repo and follow along.

---

<div align="center"><sub>MIT · <a href="https://github.com/repoachhq/repoach">github.com/repoachhq/repoach</a></sub></div>
