---
id: SP-STRUCTLOG-CACHE-TEST-FIXTURE
title: Centralize the structlog logger-cache test-order fixture instead of three duplicated copies
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: [tests/conftest.py, tests/unit/test_logging_cache_disabled_for_tests.py, tests/unit/test_selfverify_refutation.py, tests/integration/test_selfverify_refutation_flow.py, tests/unit/test_review_round2_parallel.py]
  resources: N/A

depends_on: []
provides_to: []

constraints: {}
---

# Centralize the structlog logger-cache test-order fixture instead of three duplicated copies

## Intent

Any future test module that asserts on `structlog` events via
`structlog.testing.capture_logs` against a module-level logger will
independently rediscover a serial-run-order flake — green in isolation,
red only when the full suite runs after a CLI-invoking test — because
the workaround lives as byte-identical, copy-pasted per-module `pytest`
fixtures (three of them, one further diverged into a whole-file global
`structlog.configure()` call) instead of one shared fixture. Replace all
three duplicated fixtures with a single autouse fixture in a new
`tests/conftest.py`, sitting above both `tests/unit/` and
`tests/integration/` so one definition covers both suites with zero
duplication.

## Context

- `src/repoach/core/logging.py:52`: `configure_logging()` calls
  `structlog.configure(..., cache_logger_on_first_use=True)`
  unconditionally, every time it runs. `src/repoach/cli/main.py:59` calls
  `configure_logging()` on every Typer command invocation, so any
  `CliRunner`-driven test (e.g. `tests/unit/test_chain_health.py`,
  `tests/unit/test_review_ci_mode.py`, and a dozen others under
  `tests/unit/`) flips this flag process-wide as a side effect.
- Mechanism (verified against the real `structlog` in this repo's `.venv`,
  and against the real `repoach.core.logging.configure_logging`, in a
  throwaway `pytest` file run serially): `structlog.get_logger()` returns
  a `BoundLoggerLazyProxy` whose `bind()` re-checks the GLOBAL
  `cache_logger_on_first_use` flag on every call until it has frozen once.
  The first time a given logger's `bind()` runs while the flag is `True`,
  it permanently swaps itself to a closure holding a reference to
  whichever `processors` list object `structlog.configure()` last
  installed — this is a real per-object freeze, not a lookup, so it
  ignores every future `structlog.configure()` call including
  `capture_logs()`'s own processor swap. `capture_logs()`
  (`structlog/testing.py`) mutates the CURRENT processors list in place,
  but `configure_logging()` always installs a brand-new list object
  (`shared_processors: list[Any] = [...]`, `logging.py:35-53`), so a
  logger that already froze against an OLDER list, before a LATER,
  unrelated `configure_logging()` call replaced it, writes into an
  orphaned list nobody reads — its events silently vanish from
  `capture_logs()`'s output, order-dependently.
- Commit `4c7f651` (this branch, 2026-07-22) adds a byte-identical autouse
  fixture `_fresh_selfverify_logger` to BOTH
  `tests/unit/test_selfverify_refutation.py:25-34` and
  `tests/integration/test_selfverify_refutation_flow.py:33-42`, with a
  docstring naming exactly this mechanism. The same pattern recurs a
  third time as `_fresh_orchestrator_logger` in
  `tests/unit/test_review_round2_parallel.py:86-96` (same docstring,
  different rebind target: `orchestrator_module._log`). A fourth,
  DIVERGENT variant exists at
  `tests/integration/test_chain_regen_freshness.py:29` — a bare
  module-level `structlog.configure(cache_logger_on_first_use=False)`
  statement with no shared source of truth either.
- No `tests/conftest.py` exists today (`tests/` contains only the
  `unit/` and `integration/` subpackages, each with its own
  `conftest.py`); the fixture has nowhere shared to live yet.
- `tests/unit/test_selfverify_refutation.py`, `test_review_round2_parallel.py`
  and the integration counterpart are named in the (empty) `owns.code` of
  `SP-SELFVERIFY-REFUTABLE-GAPS` and `SP-REVIEW-ROUND2-PARALLEL`
  respectively as promised-test paths; neither spec's frontmatter lists
  any file under `owns.code` (both are `[]`), so this spec's edits to
  those files (removing only the local rebind fixture and its now-unused
  `import structlog` line, changing nothing about the tests those specs'
  Acceptance Criteria named) create no ownership conflict.

## Goals

- G1: One new autouse fixture, `_disable_structlog_logger_cache_for_tests`,
  lives in a new `tests/conftest.py` and resets
  `cache_logger_on_first_use=False` before EVERY test in both
  `tests/unit/` and `tests/integration/` — a per-test reset, not a
  session-scoped run-once fixture, because `configure_logging()`
  unconditionally re-enables the flag on every call, so only re-asserting
  `False` before each individual test neutralizes every CLI-invoking test
  that ran before it.
- G2: The three duplicated per-module rebind fixtures are retired —
  `_fresh_selfverify_logger` in both
  `tests/unit/test_selfverify_refutation.py` and
  `tests/integration/test_selfverify_refutation_flow.py`, and
  `_fresh_orchestrator_logger` in
  `tests/unit/test_review_round2_parallel.py` — along with each file's
  now-unused `import structlog` line (the only remaining use in each file
  was the fixture's own `structlog.get_logger(...)` call).
- G3: All `capture_logs`-based assertions in those three files keep
  passing, unchanged, relying solely on the shared `tests/conftest.py`
  fixture.
- G4: A new regression test,
  `tests/unit/test_logging_cache_disabled_for_tests.py`, reproduces the
  exact flake mechanism end to end against the real
  `repoach.core.logging.configure_logging` and proves the shared fixture
  prevents it, run serially.

## Non-Goals

- NG1: No behavior change to `configure_logging()` itself
  (`src/repoach/core/logging.py`) — `cache_logger_on_first_use=True`
  remains for real (non-test) processes; the override lives only in the
  test-suite fixture, never touching the production code path.
- NG2: The divergent inline `structlog.configure(cache_logger_on_first_use=False)`
  statement at `tests/integration/test_chain_regen_freshness.py:29` is
  left untouched — a harmless, now-redundant no-op once the shared
  fixture also resets the flag before every test. Retiring it is a
  separate, lower-risk follow-up; folding it in here would touch a fourth
  file for no behavioral gain and dilute this spec's focus on the three
  actually-duplicated fixtures the finding names.
- NG3: No change to the `llm_proxy` subtree's `loguru`-based logging
  (`src/repoach/llm_proxy/config/logging_config.py`) — a different
  logging library entirely, not implicated in this `structlog`-specific
  mechanism.
- NG4: No broader fix for the pre-existing condition that a
  `CliRunner`-driven test's own `configure_logging()` call permanently
  mutates global `structlog` processors and stdlib logging level for the
  rest of the test session — that condition is the ROOT the flake rides
  on, but general `configure_logging()` test-isolation is out of scope
  here; this spec closes only the `cache_logger_on_first_use` freeze path
  specifically.
- NG5: No `pytest-randomly`/order-randomization plugin is added or
  removed; the new regression test's four cases rely on `pytest`'s
  existing default in-file definition order and must run serially
  (without `-n auto`) to reproduce the mechanism — noted explicitly in
  its own module docstring.

## Interface

`tests/conftest.py` (NEW FILE, one level above `tests/unit/` and
`tests/integration/` so pytest applies it to both without duplication):

```python
"""Shared test-session hermeticity defaults for both suites.

Sits above ``tests/unit/`` and ``tests/integration/`` so pytest applies
its autouse fixtures to both without duplication.
"""

from __future__ import annotations

import pytest
import structlog


@pytest.fixture(autouse=True)
def _disable_structlog_logger_cache_for_tests() -> None:
    """Reset ``cache_logger_on_first_use`` to ``False`` before every test.

    ``configure_logging`` (``src/repoach/core/logging.py``, exercised by
    any ``CliRunner``-driven test through ``cli/main.py``) unconditionally
    sets ``cache_logger_on_first_use=True`` and installs a fresh
    processors list. A bound logger whose first-ever real call lands
    while that flag is ``True`` permanently freezes a reference to
    whichever processors list is current at that instant; a later,
    unrelated ``configure_logging`` call replaces that list with a new
    object, so the frozen logger's output never reaches a subsequent
    ``structlog.testing.capture_logs`` — invisible, in a way that depends
    on serial run order. Because the flag is re-read on every unfrozen call,
    resetting it before EACH test (not once per session) is required: a
    fixture that ran only at session start would not survive any later
    CLI-invoking test re-enabling the flag for everything after it.
    """
    structlog.configure(cache_logger_on_first_use=False)
```

`tests/unit/test_selfverify_refutation.py`,
`tests/integration/test_selfverify_refutation_flow.py`,
`tests/unit/test_review_round2_parallel.py`:
- Remove the `_fresh_selfverify_logger` / `_fresh_orchestrator_logger`
  fixture definitions and each file's now-unused `import structlog` line.
  `from structlog.testing import capture_logs` stays (still used by the
  files' own test bodies).

`tests/unit/test_logging_cache_disabled_for_tests.py` (NEW FILE):
- Four ordered test functions sharing one module-level
  `_target_log = structlog.get_logger(...)`, driving the real
  `repoach.core.logging.configure_logging` to reproduce the freeze/replace
  sequence from Context, and a final `capture_logs()` assertion.

## Behavior

### Nominal

- A test suite run (serial, e.g. `pytest tests/unit/test_selfverify_refutation.py`)
  in which an earlier `CliRunner`-driven test called `configure_logging()`
  before a later test's `capture_logs()` block runs against a module
  logger for the first time: the shared fixture has reset
  `cache_logger_on_first_use=False` immediately before that later test's
  body executes, so the logger never freezes and `capture_logs()` sees
  every event, regardless of run order.

### Edge cases

- A test that itself calls `configure_logging()` AND performs its own
  module logger's very first-ever real log call, in the SAME test body,
  after that internal call: the logger can still freeze within that one
  test (the fixture cannot retroactively undo an action the test itself
  takes) — this matches production reality (a CLI test's own logging IS
  real, cache-enabled logging) and is not the flake being fixed; no
  existing suite's `capture_logs` test calls `configure_logging()` itself.
- `pytest -n auto --dist worksteal` (the CI/`ci_local.sh` invocation):
  individual test items can be distributed across worker processes with
  independent `structlog` global state, which can mask (not reproduce)
  the flake for the new regression test; this is inherent to xdist and
  noted in the new test file's own docstring — the flake and its fix are
  proven by the serial, non-`-n` invocation.

### Failure scenarios

- Pre-change code (no `tests/conftest.py`): the new regression test's
  fourth case observes an empty `capture_logs()` list — the same failure
  mode the three now-removed fixtures were hand-patching around,
  reproduced mechanically instead of by inspection.

## Acceptance Criteria

- [ ] AC1: unit — `tests/unit/test_logging_cache_disabled_for_tests.py`
  drives the real `configure_logging()` through the earlier-use /
  reconfigure / capture_logs sequence described in Context and Behavior;
  run serially (`pytest tests/unit/test_logging_cache_disabled_for_tests.py`,
  no `-n auto`), it FAILS on pre-change code (before `tests/conftest.py`
  exists) — verified: its fourth test asserts a non-empty `capture_logs()`
  list and gets `[]` pre-fix — and PASSES once `tests/conftest.py`'s
  fixture is present.
- [ ] AC2: unit — `tests/unit/test_selfverify_refutation.py`,
  `tests/integration/test_selfverify_refutation_flow.py`, and
  `tests/unit/test_review_round2_parallel.py` no longer define
  `_fresh_selfverify_logger` / `_fresh_orchestrator_logger`, no longer
  `import structlog` at module level, and their existing
  `capture_logs`-based tests still pass unchanged, relying solely on the
  shared `tests/conftest.py` fixture (verified: all three files' existing
  tests pass with the fixtures removed and only the new conftest fixture
  in place).
- [ ] AC3: promised tests —
  `tests/unit/test_logging_cache_disabled_for_tests.py::test_1_earlier_cli_invoking_test_configures_logging`,
  `::test_2_earlier_non_capture_use_of_target_logger`,
  `::test_3_another_cli_invoking_test_reconfigures`, and
  `::test_4_capture_logs_sees_the_captured_event` (the last one is the
  discriminating assertion; the first three set up the reproducing
  sequence).
- [ ] AC4: `ruff check` + `ruff format --check` + `pytest tests/unit` +
  `pytest tests/integration` (both invocations as `ci_local.sh` runs
  them, `-n auto --dist worksteal`) all green; zero inline comments
  (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`; `repoach arch graph --check`
  exits 0.

## Architecture Impact

- Adds/Removes dependency: none — `tests/conftest.py` is a new,
  test-only file with no import from `src/`; no new third-party package.
- New / changed coupling, cycles, or shared state: removes three
  duplicated, order-sensitive fixture definitions in favor of one shared
  one; reduces coupling between unrelated test modules and the
  `cache_logger_on_first_use` global to a single point of control.

## Diagram

N/A (test-infrastructure consolidation, no runtime component change).

## Open Questions

(none)
