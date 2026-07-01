<div align="center">

# 🔥 Ferova

### The self-forging software factory.

*Plug it into your repo — it builds your system, and keeps forging itself.*

[![CI](https://img.shields.io/badge/CI-green-brightgreen)]() [![License](https://img.shields.io/badge/license-MIT-blue)]() [![Built by Ferova](https://img.shields.io/badge/built%20by-Ferova-orange)]()

</div>

---

**Ferova** is an autonomous software factory. You point it at a repository and it ships changes through a multi-agent review pipeline that verifies its own work before anything merges. It runs its agent fleet over a self-healing, multi-provider LLM gateway — and it improves its own infrastructure as it goes.

> *ferro* — iron. The metal you forge, and re-forge.

## Why Ferova is different

Most "AI writes your code" tools trust the model. **Ferova trusts evidence.**

### 1. Evidence-first merge gate
Agents can't approve their own work. Every finding a reviewer raises is **re-verified against the current HEAD** before a merge is allowed. There is no forgeable "LGTM" — the gate re-checks the facts (CI green, zero surviving blockers, complete review) at merge time, every time.

### 2. Governed specs
Every change is anchored to a spec whose frontmatter declares **what it owns** and **what it depends on**. From that, Ferova *derives* the system's dependency graph — and *enforces* it: an import that crosses a boundary the spec never declared fails CI. The architecture can't silently drift from the docs.

### 3. Self-evolving routing
Ferova runs its agents across a dozen LLM providers with automatic failover. A built-in autopilot watches each provider's live health and **rewrites its own routing** to stay fast and cheap — bounded by hard safety caps.
> *It once tried to delete its own routing in a single cycle. Here's the post-mortem →* [link]

### 🔁 It builds itself
Ferova is built by Ferova. Every PR in this repo went through Ferova's own review pipeline and merge gate. **The factory is its own first customer.**

## How it works

```
spec ─▶ Developer agent ─▶ PR ─▶ Architect · Sentinel · Tester · Scribe ─▶ evidence-first gate ─▶ merge
                                          (4 reviewers)                   (re-verifies at HEAD)
```

## Quickstart

```bash
# zero-key demo against a sample repo — no provider keys required
pipx run ferova demo
```

## Safety

Letting agents write and merge code is only acceptable if the blast radius is bounded. In Ferova:

- Agents **never merge to your protected branches** — they open PRs and stop.
- Every agent fix is **path-whitelisted** — no touching CI config, workflows, or secrets.
- Secrets are **scrubbed** from every agent's environment.
- The self-evolving router is bounded by **per-cycle caps** and **can't deploy without a read**.

## Status

Early, and built in the open. If the idea resonates — an autonomous factory that's *transparent, self-evolving, and safe to be wrong* — ⭐ the repo and follow along.

---

<div align="center"><sub>MIT · <a href="https://github.com/ferovahq/ferova">github.com/ferovahq/ferova</a></sub></div>
