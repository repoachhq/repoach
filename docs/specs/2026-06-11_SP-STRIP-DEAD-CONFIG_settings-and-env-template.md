# SP-STRIP-DEAD-CONFIG — remove dead config (Settings property + stale env template)

## Metadata

- **Status**: OPEN
- **Priority**: P3
- **Owner**: operator
- **Executor**: hand-implemented (multi-agent config audit + manual removal)
- **Opened**: 2026-06-11

## Why

Builder-only sweep, config level. A multi-agent workflow audited the
config surface (the two `Settings(BaseSettings)`, the config modules, and
the env files) — 4 per-surface auditors + one adversarial skeptic per
candidate. It confirmed dead a `Settings` property, a wide band of stale
`env.example` keys (all referencing features stripped at the genesis
reset), and a long tail of orphan `.env` keys (operator-local, flag-only).

## What

1. **`config/settings.py`** — delete the `model_name` `@property`
   (`self.model.split("/", 1)[1]`): zero readers anywhere (its sibling
   `provider_type` IS read at `routes.py`, proving the access pattern
   would surface if it existed). Refresh the `Settings` class docstring —
   the upstream-provider grouping listed `DeepSeek / Kimi / LM Studio /
   Llama.cpp`, all removed; the real set is **NVIDIA NIM / OpenRouter /
   Claude Code**.
2. **`config/env.example`** — drop the stripped-feature blocks the
   template still documented (107 → 50 lines):
   - local providers: `FEROVA_LM_STUDIO_BASE_URL`, `FEROVA_LLAMACPP_BASE_URL`,
     `FEROVA_LMSTUDIO_PROXY`, `FEROVA_LLAMACPP_PROXY`, `FEROVA_DEEPSEEK_API_KEY`
     (no backing Settings field — a sanity grep caught this one the audit
     missed).
   - messaging / voice / chat: `FEROVA_MESSAGING_PLATFORM`,
     `MESSAGING_RATE_LIMIT/WINDOW`, `FEROVA_VOICE_NOTE_ENABLED`,
     `FEROVA_WHISPER_DEVICE/MODEL`, `FEROVA_HF_TOKEN`,
     `FEROVA_TELEGRAM_*`, `FEROVA_DISCORD_*`.
   - agent shell: `FEROVA_CLAUDE_WORKSPACE`, `FEROVA_ALLOWED_DIR`,
     `FEROVA_CLAUDE_CLI_BIN`, `FAST_PREFIX_DETECTION`,
     `ENABLE_NETWORK_PROBE_MOCK`, `ENABLE_TITLE_GENERATION_SKIP`,
     `ENABLE_SUGGESTION_MODE_SKIP`, `ENABLE_FILEPATH_EXTRACTION_MOCK`.
   Fix the two stale comments (the `Valid providers:` line and the
   thinking switch) so they name only the live providers.

## Kept (deliberate)

- The `llm_proxy/config/__init__.py` re-export of `Settings` /
  `get_settings` — flagged dead (consumers import the `.settings`
  submodule directly) but it is an idiomatic package-level API surface,
  not legacy residue. Left in place.
- The operator's live `.env` is untouched (it is gitignored and holds
  secrets). The audit's orphan `.env` list is reported separately, with a
  caveat: much of it is forward config for capabilities not yet built —
  NOT dead — and
  `FEROVA_KIMI_API_KEY` is in fact read by `auto-review.yml` (an audit
  false positive the workflow's own verifier caught).

## Definition of Done

- `Settings.model_name` is gone; `provider_type` still works.
- `env.example` documents only live config (the 3 supported providers,
  the model mapping, thinking, proxies, timeouts, auth token).
- `ruff` clean; full `pytest tests/unit` + integration green.

## Commit plan

1. `chore(config): drop the dead model_name property + strip stripped-feature keys from env.example`

## Risks

- None functional: `model_name` had zero readers; `env.example` is a
  template (the proxy `Settings` ignores unknown env via `extra="ignore"`).
