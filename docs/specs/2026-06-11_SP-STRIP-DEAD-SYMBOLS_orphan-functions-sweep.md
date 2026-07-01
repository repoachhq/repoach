# SP-STRIP-DEAD-SYMBOLS — delete confirmed-dead orphan symbols across src/

## Metadata

- **Status**: OPEN
- **Priority**: P3
- **Owner**: operator
- **Executor**: hand-implemented (multi-agent audit + manual removal)
- **Opened**: 2026-06-11

## Why

Builder-only sweep, finer pass: after the module-level orphans
(SP-STRIP-DEAD-MODULES) the remaining dead code is orphan *symbols*
inside live modules. A multi-agent workflow audited all of `src/` —
8 per-subsystem auditors enumerated every function/class/constant and
grepped its usage, then one adversarial skeptic per candidate tried to
prove it alive (re-exports, decorator registration, string dispatch,
tests, systemd/workflow/script entry points), defaulting to "alive" on
any doubt. 16 candidates were confirmed dead with zero disputes; a final
manual sanity grep caught one false positive.

## What

Delete the 15 genuinely-dead symbols and their cascades:

- `api/dependencies.py` — `get_provider`, `get_provider_for_type`,
  `cleanup_provider` (a self-contained process-cache island; the live
  HTTP path uses `resolve_provider(app=…)`). `_providers` docstring
  refreshed (it named the removed helpers).
- `api/models/anthropic.py` — `class Role(StrEnum)` (re-export dead-ends;
  the live `Message.role` is a `Literal`); drop the now-unused
  `from enum import StrEnum`. Cascade: `models/__init__.py` import +
  `__all__`.
- `core/anthropic/content.py` — `extract_text_from_content` (unconsumed
  re-export). Cascade: `anthropic/__init__.py` import + `__all__`.
- `core/anthropic/conversion.py` — `_tool_name`.
- `providers/rate_limit.py` — `GlobalRateLimiter.reset_instance`,
  `.remaining_wait`, `.is_blocked`, and the unused `T = TypeVar("T")`;
  drop `TypeVar` from the import.
- `review/orchestrator.py` — `review_pr_async` (the only `asyncio` user;
  drop the `import asyncio`).
- `review/coder_loop.py` — `_flatten_blocker_or_major`.
- `agent_engine/agent_loop.py` — `DEFAULT_CODER_CHAIN`,
  `DEFAULT_REASONER_CHAIN`, `PROXY_HAIKU_CHAIN`. Cascade: `__all__` +
  the module docstring (`PROXY_OPUS_CHAIN` / `PROXY_CODER_CHAIN` stay —
  imported by `planner.py` + `reviewer.py`; `DEFAULT_NIM_CHAIN` stays).

## Kept (false positive caught by the final sanity grep)

- `review/dev_runner.py::read_existing_files` — the verifier flagged it
  dead, but it is **imported and exercised by
  `tests/unit/test_review_dev_runner.py`**. Test-covered builder code is
  not dead code; left in place.

## Definition of Done

- The 15 symbols and every cascade (imports, `__all__`, docstrings) are
  gone; `ruff check` clean (no `F401`/`F822`); grep for the names finds
  no definition/import/export.
- Full `pytest tests/unit` + integration green — the definitive proof
  nothing reached them at import or test time.

## Commit plan

1. `chore: delete confirmed-dead orphan symbols across the proxy + review tree`

## Risks

- **Runtime-only usage not covered by tests** — mitigated by the
  adversarial verify pass (each skeptic checked decorators, dispatch,
  systemd/workflow/script entry points) plus the green suite. `get_*`
  helpers, the rate-limit accessors, and the chain aliases have no
  runtime caller.
