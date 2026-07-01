# SP-CI-SMOKE-REPAIR — make the llm_proxy CI smoke step real

## Metadata

- **Status**: OPEN
- **Priority**: P2
- **Owner**: operator
- **Executor**: hand-implemented (touches `.github/workflows/` — bot whitelist forbids it)
- **Opened**: 2026-06-11

## Why

The audit found the "Smoke — llm_proxy boots" step in
`.github/workflows/ci.yml` has **never executed**: a stray
`continue-on-error: true` line sits *inside* the `run:` block, so under
the runner's `bash -e` the step dies on an unknown command before
booting the proxy — and the legitimate step-level
`continue-on-error: true` then masks the failure. The proxy boot path
(`python -m ferova.llm_proxy` → settings load → uvicorn →
`/health`) is ~6.6k LOC with zero TestClient coverage; this step was
the only end-to-end boot check and it has been a silent no-op.

`scripts/ci_local.sh` has no smoke stage at all, despite claiming full
parity with ci.yml.

## What

1. **`.github/workflows/ci.yml`** — rewrite the smoke step:
   - Delete the stray in-script `continue-on-error: true` line and the
     step-level `continue-on-error: true` — the step becomes a real
     gate (the proxy is core infrastructure now; "WIP, tests
     unreliable" in the old comment is stale).
   - Export a dummy `FEROVA_ANTHROPIC_AUTH_TOKEN` and
     `FEROVA_PROXY_HOST=127.0.0.1` for the boot (forward-compatible
     with SP-PROXY-SECURE-DEFAULTS).
   - Boot in background, poll `http://127.0.0.1:8082/health` with a
     bounded retry loop (e.g. 15 × 2 s) instead of a blind `sleep 5`,
     fail the step when the loop exhausts, and kill the proxy plus dump
     its captured log on failure for diagnosis.
2. **`scripts/ci_local.sh`** — add the equivalent smoke stage to the
   full run (skipped under `--fast` and `--tests`), reusing the same
   poll-and-kill shape so local CI mirrors the workflow again.

## Files in scope

- `.github/workflows/ci.yml`
- `scripts/ci_local.sh`

## Out of scope

- Adding TestClient/unit coverage for `llm_proxy/api` (separate test
  slice).
- The 3.11/3.13 matrix gap in `ci_local.sh`.
- Any change to `auto-review.yml`.

## Smoke scenario

### Setup

A checkout with the dev extras installed and port 8082 free
(`ss -tlnp | grep :8082` empty — kill any orphan nohup proxy first).

### Execute

`scripts/ci_local.sh` (full mode), then break the proxy deliberately
(e.g. `FEROVA_PROXY_PORT=1` style misconfig or a syntax error in
`llm_proxy/__main__.py` on a scratch branch) and run it again.

### Expected

First run: the smoke stage reports the `/health` 200 and the script
exits 0. Second run: the smoke stage fails loudly with the proxy log
excerpt, non-zero exit — no silent skip in either direction.

## Definition of Done

- ci.yml smoke step contains no `continue-on-error` (in-script or
  step-level) and fails the job when `/health` never answers.
- The step passes on a healthy checkout (verified on a real PR run
  before merge).
- `ci_local.sh` full mode runs the same smoke and `--fast`/`--tests`
  skip it, with a stage banner consistent with the existing ones.
- shellcheck clean on `ci_local.sh` (CI shellcheck workflow stays
  green).
- Zero inline comments in any touched Python (none expected); shell
  comments are allowed per the gate's scope.

## Commit plan

1. `fix(ci): repair the llm_proxy smoke step (dead since introduction)`
2. `feat(ci-local): mirror the proxy smoke stage in scripts/ci_local.sh`

## Risks

- **Turning a never-run step into a gate can red CI** on a genuinely
  broken boot path the repo has not been testing — that is the purpose;
  budget one fix-forward PR if the first real run finds rot.
- **Port collisions on the runner** are unlikely (fresh VM) but the
  local stage must check 8082 availability first to avoid killing the
  operator's live systemd proxy — guard with a pre-check that refuses
  to run the smoke when 8082 is already bound, with a clear message.
