# Notes for Claude / AI agents working on this repo

## Project context

Ferova — an autonomous, self-evolving software factory. You plug it
into a repository and it ships changes through a multi-agent review
pipeline that verifies its own work before merging, and it improves
its own infrastructure as it goes.

The operational core is the **PR review factory** (`src/ferova/review/`)
— the system that builds the system. New capabilities are designed with
the maintainer and land PR by PR through the factory.

## Language & style

- **English everywhere** — code, comments, docstrings, log messages,
specs (`docs/specs/*.md`), prompts, tests, CLI help, PR descriptions,
commit messages. (Operator-facing conversational surfaces, when they
exist, are bilingual FR/EN — reply in the operator's language.)
- **Docstrings: Google style (Napoleon-compatible).**
- Strict type hints everywhere.
- Pydantic models for all data crossing module boundaries.
- No secrets in code — everything via `.env`. All env vars use the
  `FEROVA_*` prefix.

## Golden rule — zero inline comments, zero `# noqa`

(SP-NO-INLINE-COMMENTS-GATE) anywhere in `src/`, `tests/`, `scripts/`.
Substantive *why* belongs in module / class / function docstrings;
well-named identifiers carry the *what*. Per-line lint suppression is
forbidden — exceptions live in `pyproject.toml` only. Enforced at three
layers:

- **Pre-commit** — `.githooks/pre-commit` (activate once with
  `git config core.hooksPath .githooks`).
- **CI** — dedicated lint step in `.github/workflows/ci.yml` plus
  `tests/unit/test_no_inline_comments_gate.py` inside the pytest run.
- **Local one-shot** — `python scripts/lint_no_inline_comments.py`
  (add `--summary` for a single line). Same for
  `scripts/lint_no_silent_except.py` (SP-LINT-LOG-CATCH-ALL).

## Stack

- Python 3.11+ (Conda env `ferova`).
- Pydantic v2 + pydantic-settings for models and config.
- SQLAlchemy + SQLite for storage.
- Typer for the CLI, FastAPI/uvicorn for the llm_proxy sidecar.
- LLM proxy with chain failover across NIM, OpenRouter, claude_code
  (see `chains.env`).
- Keep `pyproject.toml` minimal — add deps with the code that uses them.

## Conventions

- Logging via `structlog` (JSON in prod, console in dev); the
  `llm_proxy` subtree uses `loguru`.
- Tests: `pytest` (+ `hypothesis` for future stat models).
- Lint/format: `ruff` (config in `pyproject.toml`).

## Layout

```
src/ferova/
  agent_engine/    # provider-agnostic AgentLoop (runs the review bots)
  cli/             # Typer CLI (review factory only)
  core/            # config, logging
  lint/            # no-inline-comments + no-silent-except gates
  llm/             # capability tiers (opus/sonnet/haiku)
  llm_proxy/       # chain-failover LLM proxy sidecar (port 8082)
  review/          # PR review factory (the system that builds the system)
prompts/review/    # versioned reviewer/coder/developer prompts (semver)
scripts/           # ci_local.sh, safe_merge.sh, lint gates
tests/unit/        # the suite CI requires green
```

## Branch convention

- `main` — protected, only updated by manual `develop → main` merges
  by the user.
- `develop` — protected, integration branch. PRs from feature
  branches target `develop`. The review-bot team can auto-merge
  into `develop` once verdict is APPROVE and CI is green; bots
  must NEVER auto-merge into `main`.
- Feature branches (`feat/…`, `fix/…`, `chore/…`) → PR against `develop`.

## Review-bot team

- Module: `src/ferova/review/` — 4 reviewers (Architect /
  Sentinel / Tester / Scribe) + Coder owner + **Developer** (SP-DEV),
  all via `agent_engine/agent_loop.py` over the local proxy (no
  Anthropic quota burn).
- Workflow: `.github/workflows/auto-review.yml` runs on
  `pull_request {opened,synchronize,reopened,ready_for_review}`
  against `develop`.
- CLI: `ferova review pr <N>` (run team), `ferova
  review report <N>` (fetch sticky archive comment),
  `ferova review fix <N>` (one Coder iteration),
  `ferova review merge <N>` (squash-merge gate),
  `ferova plan <spec-id>` (Planner agent → `docs/plans/<id>.md`;
  `--explore-via {proxy,claude_cli}` picks the exploration backend —
  proxy chain vs one read-only `claude -p` session on the Max quota),
  `ferova develop <spec-id>` (plan-driven Developer session
  from a spec: Planner → plan → step-by-step execution, one commit
  per step; also reachable as `ferova review develop`).
- Persistence: `pr_reviews` + `pr_coder_responses` + `pr_merges`
  tables (SQLite, `FEROVA_DB_PATH`).
- Push notification: when `CLAUDE_CODE_ROUTINE_ID` +
  `CLAUDE_CODE_ROUTINE_TOKEN` repo secrets are set, the
  orchestrator fires a routine that spawns a fresh Claude Code
  session pre-loaded with the TeamOutcome JSON.

## Autonomous spec workflow (SP-DEV)

```
docs/specs/<id>.md      ← human writes specification
        │
        │  ferova develop <id>
        ▼
Developer → push branch → PR(develop)
        │
        ▼
Architect / Sentinel / Tester / Scribe  → verdict + comments
        │
   if REQUEST_CHANGES
        ▼
Coder → ruff + pytest matrix gate → push → re-review (≤3)
        │
   if APPROVE + CI green
        ▼
auto_merge → develop  (then human merges develop → main)
```

- Spec convention: `docs/specs/<date>_<SP-ID>_<slug>.md`.
- Branch convention: `feat/sp-<id>-impl` (auto-generated);
  `detect_spec_from_branch` parses the id back out.
- Capacity limit: specs above ~500 LOC across ≥3 new files exceed
  the autonomous Developer — hand-implement those.
- Path whitelist applies to every fix the bots emit: never
  `.github/workflows/*`, `prompts/review/*`, `.env*`, no traversal.

## Useful commands

```bash
pip install -e ".[dev]"
pytest tests/unit
ruff check src tests scripts && ruff format --check src tests scripts
python scripts/lint_no_inline_comments.py --summary
python scripts/lint_no_silent_except.py --summary
ferova --help
```

## Local CI mirror (GitHub Actions budget conservation)

Run the CI gates locally before pushing:

```bash
scripts/ci_local.sh           # full parity with .github/workflows/ci.yml
scripts/ci_local.sh --fast    # lint-only (ruff + format + no-inline-comments)
scripts/ci_local.sh --tests   # pytest-only
scripts/ci_local.sh --integration # run integration tests
```

Pair with `ferova review pr <N>` to run the review-bot team
locally too.

## Branch-protection equivalent (client-side)

One-time bootstrap on a fresh clone:

```bash
git config core.hooksPath .githooks
```

- **`.githooks/pre-commit`** — ruff check + format, no-inline-comments
  lint, shellcheck on staged files.
- **`.githooks/pre-push`** — refuses any direct push to `develop` or
  `main` (PR-only on protected branches), then runs
  `scripts/ci_local.sh --fast`. Override only for hook-internal
  repairs with `git push --no-verify`.

Use **`scripts/safe_merge.sh <PR>`** instead of `gh pr merge`. It
enforces: base = `develop`, full local CI, `ferova review pr`, the
**pure evidence-first merge gate** (`ferova review gate <N>` —
re-verifies the findings ledger at head: CI green, zero blocking
findings surviving re-verification, complete review, spec coverage;
SP-VERDICT-FLIP 10a replaced the forgeable archive 4/4-APPROVE check),
then `gh pr merge --squash --delete-branch`. A refused gate prints its
reasons and blocks the merge (emergency override prompt only). Flags:
`--skip-tests` (lint-only CI), `--skip-review` (no review-bot run —
bypasses the gate) — never use them unless the operator explicitly asks.
