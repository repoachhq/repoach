# SP-REVIEW-MEMORY — review-scoped agentmemory: recall + curated seeds on the current bench

## Metadata

- **Status**: OPEN
- **Priority**: P1 (first brick of the review redesign's LEARN zone)
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-11

## Why

The review redesign (docs/review_redesign_architecture.md) gives the
REVIEW block its own memory. The *remember* side is only safe once
findings are verified (learning from today's raw reviewer comments
would teach the bench its own hallucinations) — it lands with slice 11
reading the findings ledger. The *recall* side is safe **now**: two
months of operation produced a curated corpus of review traps
(pre-existing-code flags, phantom missing-docstrings, phantom
missing-tests, COMMENT loops, truncation extrapolation) that the
current bench keeps falling into. Injecting them at review time
reduces false positives today, and the plumbing (scope, client calls,
prompt section) carries over unchanged into the redesigned bench.

`memory/agentmemory_client.py` is already scope-generic
(`project=` parameter, graceful on failure) — this spec adds the
`review` scope beside `builder`, mirroring
`review/builder_memory.py`.

## What

1. **`src/ferova/review/review_memory.py`** (new) — mirror of
   `builder_memory.py` with `REVIEW_PROJECT = "review"`:
   - `recall_review_lessons(query: str) -> list[str]` — returns `[]`
     when the kill-switch is off or the service is unreachable.
   - `review_lessons_section(lessons: list[str]) -> str` — markdown
     block titled `## Review lessons (agentmemory)` with a one-line
     preamble ("Hard-won traps from past reviews — verify before you
     flag."); `""` for an empty list.
   - `SEED_REVIEW_LESSONS: tuple[str, ...]` — the curated traps:
     1. Verify a flagged string is part of THIS diff before flagging
        it — pre-existing code (including French strings) is out of
        scope for the review.
     2. Never claim a docstring or docstring section is missing
        without reading the cited file — they are usually present.
     3. Never claim a test is missing without searching `tests/` for
        the symbol — it usually exists.
     4. A COMMENT verdict must carry concrete, actionable asks; once
        an ask is addressed, do not repeat it next round.
     5. If the diff looks truncated, say so and review only what is
        visible — never extrapolate blockers from code you cannot see.
     6. Every comment must cite a file:line that exists in the diff
        under review.
   - `seed_review_memory() -> int` — writes the seeds, returns the
     accepted count.
2. **`src/ferova/core/config.py`** — `review_memory_enabled: bool`
   Field, default `True`, alias pair
   `FEROVA_REVIEW_MEMORY_ENABLED` / `REVIEW_MEMORY_ENABLED`
   (`validation_alias=AliasChoices(...)`, NOT `alias=`), description
   marking it as the hard kill-switch for the review recall loop.
3. **`src/ferova/review/orchestrator.py`** — ONE recall per
   `review_pr` run (not one per reviewer): query built from the PR
   title plus the changed file paths (first ~10), e.g.
   `f"review {pr_title} {' '.join(paths)}"`. The rendered section is
   APPENDED to each reviewer's fully rendered prompt (after all
   placeholder substitution — no template change, no new placeholder;
   `prompts/review/*` stays untouched). Emit a
   `review_team.lessons_recalled` structlog info with `n_lessons`.
4. **`src/ferova/cli/main.py`** — mirror the builder tools on the
   existing `memory_app`: `seed-review` and `recall-review <query>`.

## Files in scope

- `src/ferova/review/review_memory.py` (new)
- `src/ferova/review/orchestrator.py`
- `src/ferova/core/config.py`
- `src/ferova/cli/main.py`
- `tests/unit/test_review_memory.py` (new)

## Out of scope

- Any `remember` write from review outcomes — slice 11
  (SP-REVIEW-LESSONS) wires it to *verified* findings only.
- Touching `prompts/review/*` templates (bot whitelist forbids it;
  the append-after-render approach makes it unnecessary).
- Reviewer-side per-lens queries (one shared recall per PR is enough
  until the ledger exists).
- The refuter (does not exist yet — redesign slice 5).

## Smoke scenario

### Setup

agentmemory service running locally on :3111 (systemd --user), repo
checkout with dev extras.

### Execute

```
ferova memory seed-review
ferova memory recall-review "docstring missing"
ferova review pr <any open PR> --dry-run
```

### Expected

Seeding reports 6 accepted. The recall returns the docstring trap.
The dry-run review log shows `review_team.lessons_recalled` with
`n_lessons >= 1`, and each reviewer prompt ends with the
`## Review lessons (agentmemory)` block. With
`FEROVA_REVIEW_MEMORY_ENABLED=false` the log line is absent and no
HTTP call is made.

## Definition of Done

- `recall_review_lessons` returns `[]` on kill-switch off AND on
  service failure (asserting-fake client, no live network in tests —
  default-network sentinel pattern).
- Seeds write 6 lessons to `project="review"` only — the fake records
  the project of every call.
- Orchestrator performs exactly ONE recall per run and appends the
  section to every reviewer prompt; prompt content asserted via the
  existing reviewer test fakes.
- `review_team.lessons_recalled` emitted (structlog capture).
- CLI `seed-review` / `recall-review` mirror the builder commands.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): review-scoped agentmemory module with curated seed traps`
2. `feat(review): orchestrator recalls review lessons once per run, appends to prompts`
3. `feat(cli): memory seed-review + recall-review commands`
4. `test(review): review memory recall, seeds, kill-switch, prompt injection`

## Risks

- **Lesson noise**: six short bullets ≈ 600 chars per prompt — far
  under the diff cap; acceptable.
- **Service down in CI**: the client is graceful (returns `[]`), the
  bench behaves exactly as today — no new CI dependency.
- **Double-injection with future redesign slices**: the append point
  moves into the finder assembly at slice 3; this module is reused
  as-is.
