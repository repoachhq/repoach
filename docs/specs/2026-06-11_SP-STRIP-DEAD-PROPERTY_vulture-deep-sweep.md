# SP-STRIP-DEAD-PROPERTY — vulture-deep sweep (dead branches / locals / params / attributes)

## Metadata

- **Status**: OPEN
- **Priority**: P3
- **Owner**: operator
- **Executor**: hand-implemented (vulture + ruff scan, manual verify)
- **Opened**: 2026-06-11

## Why

Final, finest pass of the builder-only sweep: dead code *inside* live
functions — unreachable branches, unused locals, unused parameters,
unused instance attributes — below the symbol level that
SP-STRIP-DEAD-SYMBOLS reached.

Tooling: `ruff` (F841 unused-local, RET unreachable/redundant-return,
B007) over `src/` and `vulture --min-confidence 60`. Every candidate was
manually verified.

## Result — the codebase is clean at this level

- **ruff**: zero unused locals (F841=0), zero genuine unreachable code.
  (Two cosmetic RET nits left untouched: an explicit `return None` and a
  named-result `return s` — both defensible readability, and RET is not
  in the project ruff config.)
- **vulture ≥70% confidence**: one hit, a **false positive** —
  `providers/base.py` `if False: yield ""` is the deliberate idiom that
  makes an abstract async-generator type-check under mypy/ty (the
  docstring says so). Kept.
- **vulture 60%**: 90 hits, all verified false positives except one:
  - 29 "unused variable" → all Pydantic / dataclass fields (serialised
    or validated via the framework) and module constants. None are
    genuine unused locals (ruff confirms F841=0).
  - 52 "unused function/method" → already adversarially verified by
    SP-STRIP-DEAD-SYMBOLS; the remainder are routes / commands /
    validators / abstract methods.
  - 7 "unused attribute" + 1 "unused property": `_model_chain`,
    `_per_call_timeout_s`, `_allowed_tools` (×3), `failed_step_index`
    are all read (several in tests); `logging.root.handlers` is the
    stdlib logging API, not a custom attribute. **Only one was genuinely
    dead.**
- **ruff ARG (unused parameters)**: 17 hits, **all interface
  conformance** — FastAPI exception-handler `request`, a Pydantic
  validator `info`, and the shared provider-method contract
  (`anthropic_messages` / `open_router` / `openai_compat` override the
  same signatures; `openai_compat` exposes overridable no-op hooks).
  Removing any would break the signature. All kept.

## What

Delete the one genuinely-dead item:

- **`core/anthropic/thinking.py`** — the `in_think_mode` `@property` on
  `ThinkTagParser` (returns `self._in_think_tag`): zero callers anywhere.
  The internal `_in_think_tag` state stays (used by `feed`).

## Definition of Done

- `in_think_mode` gone; `vulture --min-confidence 70` over `src/` now
  reports only the type-checker idiom (a known FP).
- `ruff` clean; full `pytest tests/unit` + integration green.

## Commit plan

1. `chore(proxy): remove the unused in_think_mode property (vulture-deep sweep)`

## Risks

- None: zero callers; internal state untouched.
