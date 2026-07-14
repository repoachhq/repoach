# Getting started

This guide takes you from `git clone` to a live, authenticated request
through Ferova's LLM gateway, then through the CLI and (optionally) the
GitHub review factory. Every command is copy-pasteable; expected output
is shown where it matters.

Two reading paths:

- **Local live test** (sections 1–8): install, configure one provider,
  start the gateway, make a real completion call, run the CLI and the
  test suite. No GitHub required.
- **Full factory** (section 9): point the review factory at a GitHub
  repository — reviewers, merge gate, autonomous development.

## 1. Prerequisites

| Requirement | Why | Check |
|---|---|---|
| Python ≥ 3.11 | `requires-python` in `pyproject.toml`; CI tests 3.11 and 3.13 | `python --version` |
| git | everything | `git --version` |
| curl | live gateway test | `curl --version` |
| An API key for at least one supported provider | the gateway needs one live upstream | see section 4 |
| `gh` CLI, authenticated | **only** for section 9 (review factory) | `gh auth status` |
| shellcheck | **optional** — pre-commit hook lints staged `.sh` files when present | `shellcheck --version` |

Supported providers (the gateway routes to these):
`nvidia_nim`, `open_router`, `kimi`, `groq`, `cerebras`, `deepseek`,
and `claude_code` (uses a local `claude` CLI as last-resort fallback —
optional, chains work without it).

## 2. Clone and install

```bash
git clone https://github.com/ferovahq/ferova && cd ferova
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Verify:

```bash
ferova version
ferova --help
```

`ferova --help` lists the CLI surface; every subcommand has its own
`--help`.

## 3. Activate the guardrails (one-time)

```bash
git config core.hooksPath .githooks
```

This wires two hooks:

- **pre-commit** — `ruff check` + `ruff format --check`, the
  no-inline-comments and no-silent-except gates, `ferova arch check
  --staged`, and shellcheck on staged shell files (skipped if
  shellcheck is not installed).
- **pre-push** — refuses direct pushes to `develop`/`main` (PR-only)
  and runs the fast lint mirror (`scripts/ci_local.sh --fast`).

You only need the hooks if you intend to contribute; skip this step
for a read-only evaluation.

## 4. Configure secrets — `.env`

Secrets never live in code. Copy the template and fill it in:

```bash
cp .env.example .env
```

The two values you must set for a live test:

```dotenv
FEROVA_ANTHROPIC_AUTH_TOKEN=pick-any-long-random-string
NVIDIA_NIM_API_KEY=nvapi-...
```

- **`FEROVA_ANTHROPIC_AUTH_TOKEN`** is the gateway's shared secret —
  the "login" of this stack. You invent it (e.g. `openssl rand -hex
  32`); every client of the gateway (curl, the agents, the CLI)
  authenticates with this exact value. The agent engine refuses to
  start without it, and the proxy refuses to bind a non-loopback host
  without it.
- **One provider key** matching the chain heads you will route to
  (section 5). All provider keys accept both spellings — bare
  (`NVIDIA_NIM_API_KEY`) and prefixed (`FEROVA_NVIDIA_NIM_API_KEY`):

| Provider | Key variable | Where to get one |
|---|---|---|
| NVIDIA NIM | `NVIDIA_NIM_API_KEY` | build.nvidia.com (free tier) |
| OpenRouter | `OPENROUTER_API_KEY` | openrouter.ai |
| Kimi / Moonshot | `KIMI_API_KEY` | platform.moonshot.ai |
| Groq | `GROQ_API_KEY` | console.groq.com |
| Cerebras | `CEREBRAS_API_KEY` | cloud.cerebras.ai |
| DeepSeek | `DEEPSEEK_API_KEY` | platform.deepseek.com |

Useful knobs already in `.env.example` (defaults shown):

```dotenv
FEROVA_ENV=dev                    # prod additionally REQUIRES the auth token at boot
FEROVA_LOG_LEVEL=INFO
FEROVA_DB_PATH=./data/ferova.db   # SQLite; auto-created on first use
FEROVA_PROXY_HOST=127.0.0.1       # non-loopback binds refuse to boot without a token
FEROVA_PROXY_PORT=8082
FEROVA_LLM_PROXY_BASE_URL=http://localhost:8082
```

Precedence, lowest to highest: `chains.env` → `.env` → real
environment variables. An exported shell variable always wins.

## 5. Model routing — `chains.env`

`chains.env` is the single source of truth for which upstream models
serve each capability tier. Three lines, one per tier:

```dotenv
MODEL_OPUS=nvidia_nim/z-ai/glm-5.2,open_router/z-ai/glm-5.2,claude_code/opus
MODEL_SONNET=nvidia_nim/deepseek-ai/deepseek-v4-pro,claude_code/sonnet
MODEL_HAIKU=nvidia_nim/z-ai/glm-5.2,claude_code/haiku
```

- Each value is a **failover chain**: comma-separated
  `provider/model` references, walked left to right on rate-limit,
  error, or parse failure.
- The router picks a chain by **substring match** on the model name a
  client sends: a request for any model containing `sonnet` walks
  `MODEL_SONNET`, and so on.
- A single-entry chain is valid. Minimal single-provider setup (one
  NIM key, no `claude` CLI needed):

```dotenv
MODEL_OPUS=nvidia_nim/z-ai/glm-5.2
MODEL_SONNET=nvidia_nim/deepseek-ai/deepseek-v4-pro
MODEL_HAIKU=nvidia_nim/z-ai/glm-5.2
```

The committed `chains.env` is a working configuration — if you have
keys for its head providers you can leave it untouched.

## 6. Start the gateway and log in

The gateway is the heart of the stack: one local endpoint, multiple
providers behind it, automatic failover, circuit breakers, telemetry.

**Terminal 1 — start it:**

```bash
python -m ferova.llm_proxy
```

It binds `127.0.0.1:8082` by default (from your `.env`).

**Terminal 2 — verify it's up** (health is unauthenticated by design):

```bash
curl -sf http://127.0.0.1:8082/health
```

```json
{"status": "healthy", "breaker": []}
```

**Make your first authenticated, live completion.** Authentication is
your `FEROVA_ANTHROPIC_AUTH_TOKEN`, sent as `x-api-key` (also
accepted: `Authorization: Bearer <token>`):

```bash
source .env
curl -sS http://127.0.0.1:8082/v1/messages \
  -H "x-api-key: $FEROVA_ANTHROPIC_AUTH_TOKEN" \
  -H "content-type: application/json" \
  -d '{
        "model": "sonnet",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "Reply with exactly: ferova gateway live"}]
      }'
```

You get an Anthropic-style streaming response served by whatever
provider heads your `MODEL_SONNET` chain. A wrong token returns 401 —
that's your login check.

The capability-native endpoint used by Ferova's own agents is
`POST /v1/agent` and takes a tier instead of a model name:

```bash
curl -sS http://127.0.0.1:8082/v1/agent \
  -H "x-api-key: $FEROVA_ANTHROPIC_AUTH_TOKEN" \
  -H "content-type: application/json" \
  -d '{"capability": "haiku", "messages": [{"role": "user", "content": "ping"}]}'
```

## 7. CLI tour — what works right now

With the gateway running and no GitHub configured:

```bash
ferova chains-audit        # classify each chain head (offline, always exits 0)
ferova monitor-chains      # LIVE probe of each tier's head; persists to SQLite
ferova review insights     # findings-ledger stats from the local DB
ferova arch check          # spec-governance gate over your working tree
```

`ferova monitor-chains` is the best live smoke test: it sends one real
probe per tier and prints a per-tier status line (`ok` / `slow` /
`error`), then exits non-zero if any tier is degraded — the same
command the production timer runs every 15 minutes.

The SQLite database (`./data/ferova.db`) is created automatically on
first use — there is no migration or init step.

## 8. Verify the installation

```bash
scripts/ci_local.sh --fast   # lint gates only (seconds)
scripts/ci_local.sh          # full parity with CI: lint + unit tests + proxy smoke
pytest -q tests/unit         # the suite CI requires green
```

The full run boots its own throwaway proxy instance for the smoke
stage and skips that stage if port 8082 is already in use (your
Terminal 1 instance can stay up).

## 9. Optional — wire up the GitHub review factory

Everything above is local. The review factory operates on GitHub pull
requests and needs:

```bash
gh auth login
```

Then, from a checkout of a repository with an open PR:

```bash
ferova review pr <N>       # run the 4 reviewers (Architect/Sentinel/Tester/Scribe)
ferova review report <N>   # fetch the sticky review archive comment
ferova review gate <N>     # evidence-first merge gate — read-only, fact by fact
ferova plan <SP-ID>        # Planner: spec -> docs/plans/<SP-ID>.md (no GitHub needed)
ferova develop <SP-ID>     # Planner -> Developer -> branch -> PR
```

`ferova develop --no-push` runs the whole build without touching
GitHub — useful for a first try.

To run the **CI-side automation on your own fork** (auto-review on PR
open, event-driven merge), you additionally need:

1. a **self-hosted GitHub Actions runner** (all workflows declare
   `runs-on: self-hosted`);
2. provider keys as **repository secrets** (`NVIDIA_NIM_API_KEY`,
   `OPENROUTER_API_KEY`, … — same names as section 4);
3. your GitHub username in the **actor allowlists** inside
   `.github/workflows/*.yml` (they ship pinned to the maintainer);
4. optionally `CLAUDE_CODE_ROUTINE_ID` / `CLAUDE_CODE_ROUTINE_TOKEN`
   secrets for push notifications into a Claude Code session.

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401` on `/v1/messages` | token mismatch — the `x-api-key` you send must equal `FEROVA_ANTHROPIC_AUTH_TOKEN` in the proxy's environment. Restart the proxy after editing `.env`. |
| Proxy refuses to boot on a non-loopback host | by design: set `FEROVA_ANTHROPIC_AUTH_TOKEN` before exposing beyond `127.0.0.1`. |
| `FEROVA_ENV=prod` boot failure | prod requires the auth token at construction time. |
| Port 8082 already bound | another instance (or another service) owns it — set `FEROVA_PROXY_PORT` and `FEROVA_LLM_PROXY_BASE_URL` together. |
| Hard error mentioning `NIM_ENABLE_THINKING` | the variable was renamed — use `ENABLE_THINKING`. |
| `AgentLoop` raises `FEROVA_ANTHROPIC_AUTH_TOKEN is missing` | any agent command (`review pr`, `develop`, …) needs the token even in dev — set it in `.env`. |
| All chain probes `error` in `monitor-chains` | the head provider's key is missing/invalid in `.env`, or the provider is down — try `curl /health` and check the breaker list. |
| A commit is rejected with an inline-comment error | the repo enforces a zero-inline-comments gate (`CLAUDE.md`) — move the *why* into a docstring. |
