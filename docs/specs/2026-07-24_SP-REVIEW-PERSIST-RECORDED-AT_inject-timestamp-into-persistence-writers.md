---
id: SP-REVIEW-PERSIST-RECORDED-AT
title: Accept an injectable recorded_at on the five persistence.py writers
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: [src/repoach/review/persistence.py, tests/unit/test_review_persistence_recorded_at.py]
  resources: N/A

depends_on: []
provides_to: []

constraints: {}
---

# Accept an injectable recorded_at on the five persistence.py writers

## Intent

The five `pr_reviews`/`pr_merges`/`pr_hallucinations`/`pr_review_dialogue`/
`pr_coder_responses` writers in `src/repoach/review/persistence.py` each
compute their own `created_at` internally via `datetime.now(UTC)`, so
neither a caller nor a test can pin a row's timestamp to a known value
without monkeypatching the `datetime` module. This is inconsistent with the
clock-injection convention the rest of the codebase already follows for
persistence writes — `src/repoach/health/store.py:record_probes` and
`src/repoach/llm_proxy/providers/cell_probe_store.py:record_cell_probes`
both take a required `recorded_at` captured at the call site, and
`src/repoach/llm_proxy/routing/breaker_persist.py` documents its
`wall_clock_now` parameter for exactly this reason. Add an optional
`recorded_at: datetime | None = None` parameter to each of the five
`persistence.py` writers, defaulting to `datetime.now(UTC)` only when the
caller does not supply one, so review-history assertions and any future
caller needing deterministic timestamps can pin `created_at` directly —
with zero change to any existing call site.

## Context

Confirmed still present on `develop`
(`git show origin/develop:src/repoach/review/persistence.py`):

- `persistence.py:235` — `record_review`, `created_at=datetime.now(UTC)`.
- `persistence.py:303` — `record_merge`, `created_at=datetime.now(UTC)`.
- `persistence.py:368` — `record_hallucination`, `created_at=datetime.now(UTC)`.
- `persistence.py:424` — `record_dialogue`, `created_at=datetime.now(UTC)`.
- `persistence.py:500` — `record_coder_response`, `created_at=datetime.now(UTC)`.

None of the five signatures (`record_review` at line 199, `record_merge` at
272, `record_hallucination` at 342, `record_dialogue` at 394,
`record_coder_response` at 478) currently exposes any timestamp parameter.

The injectable pattern this spec follows already exists twice in the
codebase:

- `src/repoach/health/store.py:94-99` — `record_probes(db_path, probes, *,
  recorded_at: datetime)` — required, call-site-captured, documented as
  "Passed in (not read from the wall clock here) to keep the store
  deterministic under test."
- `src/repoach/llm_proxy/routing/chain_regen.py:371` and
  `src/repoach/cli/main.py:135` both call their respective writers with
  `recorded_at=datetime.now(UTC)` captured at the call site.
- `src/repoach/llm_proxy/routing/breaker_persist.py:108,176` documents its
  own `wall_clock_now: datetime` parameter the same way.

`persistence.py` is not listed under `owns.code` in any existing spec
frontmatter (`grep -rl "review/persistence.py" docs/specs/*.md` matches
only `SP-CHAINPILOT-CODER-OUTCOMES`, which owns `coder_outcomes.py` and
merely reads `persistence.py` in prose, not in `owns.code`) — it is
currently unowned and free to claim here.

All five writers have existing callers that omit any timestamp argument
today: `orchestrator.py:760` (`record_review`), `orchestrator.py:766`
(`record_hallucination`), `orchestrator.py:562,585` (`record_dialogue`),
`auto_merge.py:799` (`record_merge`), and `dev_runner.py:1170,1296,1669` /
`coder_findings.py:571` (`record_coder_response`), plus their unit-test
callers. Because the new parameter is optional and defaults to the exact
current behavior, none of these call sites need to change.

## Goals

- G1: `record_review`, `record_merge`, `record_hallucination`,
  `record_dialogue`, and `record_coder_response` each accept a new
  keyword-only `recorded_at: datetime | None = None` parameter.
- G2: when `recorded_at` is `None` (the default, and the behavior at every
  existing call site), each writer computes `datetime.now(UTC)` exactly as
  it does today — byte-identical behavior for every unmodified caller.
- G3: when `recorded_at` is supplied, each writer stamps the row's
  `created_at` column with that exact value instead of calling
  `datetime.now(UTC)`.

## Non-Goals

- NG1: no behavior change beyond the new optional parameter — no existing
  call site is modified, no column, index, or table shape changes, no
  change to any function's positional arguments or return type.
- NG2: no change to `fetch_merged_pr_shas` or `fetch_dialogue` (the two
  read-side functions in the same module) — they already read back
  whatever `created_at` was stored, verbatim.
- NG3: no change to `health/store.py`, `cell_probe_store.py`, or
  `breaker_persist.py` — those already follow the target pattern and are
  cited only as precedent.
- NG4: no attempt to make `recorded_at` required — unlike
  `record_probes`, these five writers keep the default so zero callers
  are forced to change.

## Interface

`src/repoach/review/persistence.py`:

```python
def record_review(
    db_path: Path,
    *,
    pr_number: int,
    outcome: ReviewerOutcome,
    recorded_at: datetime | None = None,
) -> None: ...

def record_merge(
    db_path: Path,
    *,
    pr_number: int,
    outcome: str,
    base_ref: str,
    head_ref: str,
    merged_sha: str | None,
    notes: str,
    recorded_at: datetime | None = None,
) -> None: ...

def record_hallucination(
    db_path: Path,
    *,
    pr_number: int,
    event: GuardEvent,
    recorded_at: datetime | None = None,
) -> None: ...

def record_dialogue(
    db_path: Path,
    *,
    pr_number: int,
    round: str,
    speaker: str,
    payload: Mapping[str, Any],
    recorded_at: datetime | None = None,
) -> None: ...

def record_coder_response(
    db_path: Path,
    *,
    pr_number: int,
    plan: dict,
    model_used: str,
    elapsed_s: float,
    tokens_used: int,
    recorded_at: datetime | None = None,
) -> None: ...
```

Each writer's body replaces its single `created_at=datetime.now(UTC)`
value expression with `created_at=recorded_at or datetime.now(UTC)`.

## Behavior

### Nominal

- A caller invokes any of the five writers exactly as it does today (no
  `recorded_at` argument) → the inserted row's `created_at` is
  `datetime.now(UTC)` captured inside the writer, identical to
  pre-change behavior.
- A caller passes `recorded_at=some_datetime` → the inserted row's
  `created_at` is exactly `some_datetime`, and no call to
  `datetime.now(UTC)` occurs for that field.

### Edge cases

- `recorded_at` supplied as a naive (non-tz-aware) `datetime` → passed
  through unchanged to the `DateTime(timezone=True)` column exactly as
  any other `datetime` value would be; no new validation is introduced
  (matches the existing precedent in `record_probes`, which also accepts
  whatever `datetime` the caller passes).
- Multiple writers called in a loop (e.g. `orchestrator.py`'s per-outcome
  `record_review` loop) with no `recorded_at` → each call independently
  computes its own `datetime.now(UTC)`, exactly as today; this spec does
  not introduce a shared timestamp across a batch (that would be a
  separate, call-site-level change outside this spec's scope).

### Failure scenarios

- None introduced — the parameter is purely additive and optional; any
  failure mode of the underlying SQLAlchemy `insert(...)` call is
  unchanged from pre-change behavior.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `persistence.py`, which this spec now owns outright (previously
  unowned by any spec's `owns.code`); no new cross-module import (the
  `datetime` import already exists in the module).
- New / changed coupling, cycles, or shared state: none — purely
  additive optional parameters; no existing caller changes.

## Diagram

N/A (in-place fix, no new components or call graph edges).

## Acceptance Criteria

- [ ] AC1: unit — for each of the five writers, calling it with an
  explicit `recorded_at=<fixed datetime>` results in a persisted row
  whose `created_at` equals that fixed value (read back via a direct
  `SELECT` against the relevant table, following the pattern in
  `tests/unit/test_review_merge_persistence.py`).
- [ ] AC2: unit — for each of the five writers, calling it with NO
  `recorded_at` argument (the pre-change call shape) still succeeds and
  persists a `created_at` value; assert it falls within a tight
  `[before, after]` wall-clock bracket taken immediately around the call,
  proving the `datetime.now(UTC)` default path still fires unchanged.
- [ ] AC3: promised test file —
  `tests/unit/test_review_persistence_recorded_at.py` with selectors
  `test_record_review_accepts_injected_recorded_at`,
  `test_record_merge_accepts_injected_recorded_at`,
  `test_record_hallucination_accepts_injected_recorded_at`,
  `test_record_dialogue_accepts_injected_recorded_at`,
  `test_record_coder_response_accepts_injected_recorded_at`, and
  `test_all_five_writers_default_to_now_when_recorded_at_omitted`. Every
  one of these MUST FAIL against the pre-change signatures (`TypeError:
  unexpected keyword argument 'recorded_at'` for the first five) on the
  current `develop` code.
- [ ] AC4: existing callers unmodified — `git diff` against this spec's
  branch touches only `src/repoach/review/persistence.py` and the new
  test file; no line in `orchestrator.py`, `auto_merge.py`,
  `dev_runner.py`, or `coder_findings.py` changes.
- [ ] AC5: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `repoach arch graph --check` (or the repo's equivalent) exits 0.

## Open Questions

(none)
