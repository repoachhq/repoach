# SP-CI-SELF-HOSTED — run the whole pipeline on a free self-hosted runner

## Metadata

- **Status**: OPEN
- **Priority**: P0 — the operator has no GitHub Actions budget; the
  GitHub-hosted runners bill minutes and every workflow currently
  fails at the billing wall
- **Owner**: operator
- **Executor**: hand-implemented (`.github/workflows/*` — bot whitelist
  forbids it; force-majeure)
- **Opened**: 2026-06-13

## Why

GitHub bills minutes only for **GitHub-hosted** runners. A
**self-hosted** runner on the operator's own machine is free, and that
machine already hosts everything the pipeline needs (the NIM proxy on
systemd `:8082`, the repo, the conda env). Moving the three workflows
to `runs-on: self-hosted` preserves the entire event-driven design —
auto-review.yml, the in-run fix loop (SP-CI-FIX-LOOP-CLOSURE), the
actor gates (SP-CI-SECRETS-ISOLATION) — at zero cost, instead of
rebuilding the loop as a local CLI orchestrator.

One adaptation is required: each CI job boots its own ephemeral LLM
proxy. On the throwaway ubuntu-hosted VM that bound `:8082` freely; on
the operator's machine `:8082` is the **live systemd proxy** (the
"nohup proxy squats systemd port" hazard). The CI proxy moves to
`:8083` so it never collides with the operator's running one.

## What

In all three workflow files (`auto-review.yml`, `ci.yml`,
`shellcheck.yml`):

1. `runs-on: ubuntu-latest` → `runs-on: self-hosted` (5 occurrences).

In `auto-review.yml` and `ci.yml` (the proxy-bearing jobs):

2. Every CI-proxy reference moves off the live port: `FEROVA_PROXY_PORT:
   "8082"` → `"8083"`, every `http://127.0.0.1:8082/health` →
   `:8083`, every `FEROVA_LLM_PROXY_BASE_URL: "http://127.0.0.1:8082"`
   → `:8083`. The job still boots, health-checks and kills its OWN
   proxy — only the port changes, so the operator's systemd `:8082`
   is untouched.

The actor gates, the `bots` environment, the secret wiring, the
base-ref tooling and the in-run fix loop are all unchanged — they work
identically on a self-hosted runner.

## Files in scope

- `.github/workflows/auto-review.yml`
- `.github/workflows/ci.yml`
- `.github/workflows/shellcheck.yml`

## Operator setup (machine side, not repo code — documented here)

A self-hosted runner must be registered once on the operator's
machine, ideally as a `systemctl --user` service (matching the proxy):

```
gh api -X POST repos/<owner>/<repo>/actions/runners/registration-token --jq .token
# download + ./config.sh --url <repo-url> --token <T> --unattended --labels self-hosted
# run via ./run.sh, or a ~/.config/systemd/user unit for persistence
```

The runner must be online for jobs to leave the queue; while it is
offline, PRs simply queue (no cost) and `safe_merge.sh` still merges
locally.

## Out of scope

- Reusing the operator's warm `:8082` proxy instead of booting a CI
  one (would need the proxy token as a shared secret — optimisation
  for later if cold-start flakiness bites).
- Any change to the review/merge logic.
- Disabling/deleting workflows (we are keeping them, just relocating
  where they run).

## Smoke scenario

### Setup

The self-hosted runner registered and online; this change merged to
develop.

### Execute

A `workflow_dispatch` (or a scratch PR) against develop.

### Expected

The run is picked up by the self-hosted runner (not a GitHub-hosted
one), the CI proxy binds `:8083` without disturbing the live `:8082`,
the bench runs over NIM, and the run completes green — billed zero
GitHub minutes.

## Definition of Done

- All five `runs-on` are `self-hosted`.
- No `8082` remains in any workflow proxy reference; `8083` everywhere
  the CI proxy is booted, health-checked or addressed.
- `actionlint`/YAML parse clean.
- One real self-hosted run completes green end to end.

## Commit plan

1. `fix(ci): run all workflows on a self-hosted runner`
2. `fix(ci): CI proxy binds :8083 so it never collides with the live :8082`

## Risks

- **Runner offline → jobs queue forever**: harmless (no cost,
  cancellable); `safe_merge.sh` remains the local merge path
  meanwhile.
- **Self-hosted runs PR code on the operator's box**: contained by the
  existing owner actor gate + `bots` environment; acceptable for a
  solo private repo.
- **Machine must be on**: the operator already drives from this
  machine and the proxy already lives here.
