---
id: SP-TRUNCATE-CAP-NAMED-CONST
title: Name the duplicated 32000-char truncation caps instead of three bare literals
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code:
    - tests/unit/test_truncate_cap_named_const.py
  resources: N/A

depends_on: [SP-RETRY-BACKOFF-DEDUP, SP-REVIEW-PERSIST-RECORDED-AT]
provides_to: []

constraints: {}
---

# Name the duplicated 32000-char truncation caps instead of three bare literals

## Intent

The literal `32000` is written inline at three call sites across two
modules with no named constant anywhere. Two of the three sites
(`persistence.py`) are the same concern — an anti-bloat cap on JSON
text before it is written into a SQLite `String` column — and should
share ONE named constant so a future change to that cap is made once,
by grep-proof construction, instead of by hoping both `[:32000]`
literals are found and edited together. The third site
(`reviewer.py`) is a *different* concern — an LLM prompt-context
budget on existing-file contents shown to the reviewer/developer
prompt, already independently justified and tuned in its own
docstring — and gets its OWN distinctly named constant rather than
importing the persistence-module constant, so the two unrelated caps
cannot be silently coupled by a later "just change the shared
constant" edit.

## Context

- `src/repoach/review/persistence.py:423` — `record_dialogue`:
  `payload_json=json.dumps(dict(payload), default=str)[:32000]`.
- `src/repoach/review/persistence.py:499` — `record_coder_response`:
  `fixes_json=json.dumps(plan.get("fixes", []) or [])[:32000]`.
  Both columns are declared `Column(..., String, nullable=False)`
  (`persistence.py:80`, `:106`) — SQLAlchemy `String` with no length
  maps to an unbounded SQLite `TEXT` column, so `32000` here is a
  hand-picked anti-bloat cap, not an actual database column-width
  limit; nothing today names or documents that intent, and the two
  sites can silently drift apart (e.g. a fix to one during a future
  edit that misses the other, re-introducing an inconsistency between
  what gets truncated on write and what a caller assumes on read).
- `src/repoach/review/reviewer.py:1867-1869` — `_format_existing_files`:
  `capped = contents[:32000]` / `if len(contents) > 32000: ...`. The
  function's own docstring (`reviewer.py:1851-1860`) explains this cap
  is an LLM prompt-size budget ("32 KB covers every existing file in
  the review module ... stays well under the NIM prompt budget"),
  bumped once already from 8 KB after a truncation regression broke a
  ruff docstring gate. This is a genuinely separate tuning knob from
  the two DB-write caps above; it shares the same numeric value today
  only by coincidence.
- `reviewer.py` does not currently import anything from
  `persistence.py` (verified: no `persistence` reference anywhere in
  `reviewer.py`). Importing a persistence-module constant into
  `reviewer.py` purely to deduplicate an unrelated number would add a
  new, semantically unjustified module-dependency edge from the
  agent-orchestration module onto the storage module.
- `reviewer.py` is owned by `SP-RETRY-BACKOFF-DEDUP` (its `owns.code`
  lists the whole file); this spec depends on it rather than
  re-claiming ownership, and edits only `_format_existing_files`,
  which `SP-RETRY-BACKOFF-DEDUP` does not touch (that spec extracts
  `Reviewer._call_with_retry` / `Developer._call_with_retry`).

## Goals

- G1: `persistence.py` gains one module-level, docstringed constant
  (e.g. `_PERSISTED_JSON_TRUNCATE_CHARS = 32000`) naming the two write
  sites it backs and stating plainly that it is an anti-bloat cap on
  serialized JSON text before a `String`/`TEXT` column write, not an
  enforced SQLite column-width limit.
- G2: both `persistence.py` call sites (`record_dialogue`'s
  `payload_json`, `record_coder_response`'s `fixes_json`) read that
  one constant instead of repeating the bare literal `32000`.
- G3: `reviewer.py`'s `_format_existing_files` gains its own,
  separately named module-level constant (e.g.
  `_EXISTING_FILE_PROMPT_TRUNCATE_CHARS = 32000`), replacing its two
  bare `32000` literals, with the existing docstring rationale
  (prompt-budget, not DB-column) preserved or folded into the
  constant's own docstring/comment-free explanation.
- G4: no cross-module import is introduced between `reviewer.py` and
  `persistence.py` — the two concerns keep two names and two values
  that happen to start equal, not one shared symbol.

## Non-Goals

- NG1: no behavior change beyond the refactor itself — all three
  truncation lengths remain exactly 32000 characters, so persisted
  DB rows and the rendered prompt block are byte-identical to
  pre-change output for every input this spec's tests exercise.
- NG2: no change to the SQLAlchemy column types (`String`) or any
  schema migration — the columns stay unbounded `TEXT`; this spec only
  names the application-level cap already imposed on them.
- NG3: no change to `Reviewer._call_with_retry` /
  `Developer._call_with_retry` or any other code inside
  `SP-RETRY-BACKOFF-DEDUP`'s scope in `reviewer.py`.
- NG4: no new shared-constants module — each constant lives beside the
  code that uses it (`persistence.py` and `reviewer.py` respectively),
  matching this spec's explicit non-goal of coupling the two concerns.

## Interface

`src/repoach/review/persistence.py`:
- New module-level constant near the top of the file (after existing
  imports/column definitions, before `record_dialogue`):
  ```python
  _PERSISTED_JSON_TRUNCATE_CHARS = 32000
  ```
  with a docstring/comment-free Google-style module or constant
  docstring naming `record_dialogue`'s `payload_json` and
  `record_coder_response`'s `fixes_json` as the two call sites it
  backs, and stating it is an anti-bloat cap, not a column-width
  limit.
- `record_dialogue`: `payload_json=json.dumps(dict(payload),
  default=str)[:_PERSISTED_JSON_TRUNCATE_CHARS]`.
- `record_coder_response`: `fixes_json=json.dumps(plan.get("fixes",
  []) or [])[:_PERSISTED_JSON_TRUNCATE_CHARS]`.

`src/repoach/review/reviewer.py`:
- New module-level constant near `_format_existing_files`:
  ```python
  _EXISTING_FILE_PROMPT_TRUNCATE_CHARS = 32000
  ```
- `_format_existing_files` uses
  `contents[:_EXISTING_FILE_PROMPT_TRUNCATE_CHARS]` and
  `len(contents) > _EXISTING_FILE_PROMPT_TRUNCATE_CHARS` in place of
  the two bare `32000` literals; the function's existing docstring
  (prompt-budget rationale) is preserved.

## Behavior

### Nominal

- A dialogue payload or coder fixes list that serializes to fewer
  than 32000 characters is persisted unchanged (no truncation), same
  as today.
- Existing-file contents under the cap render unchanged in the
  reviewer/developer prompt block, same as today.

### Edge cases

- A payload/fixes JSON serialization of exactly
  `_PERSISTED_JSON_TRUNCATE_CHARS` characters is persisted whole (no
  off-by-one truncation) — same slicing semantics as the current
  literal.
- A payload/fixes JSON serialization one character over the cap is
  truncated to exactly `_PERSISTED_JSON_TRUNCATE_CHARS` characters.
- An existing-file's contents one character over
  `_EXISTING_FILE_PROMPT_TRUNCATE_CHARS` is truncated to exactly that
  length plus the existing `"[... file truncated ...]"` marker,
  unchanged from current behavior.

### Failure scenarios

- N/A — this is a pure refactor of literal-to-named-constant; no new
  failure mode is introduced. If either constant is imported and
  found missing (pre-change code), that is the discriminating signal
  this spec's tests use (AC1/AC2 below).

## Architecture Impact

- Adds/Removes dependency: none. `reviewer.py` gains NO new import
  from `persistence.py` — this is the explicit point of G4/NG4: two
  independent constants, not a shared one, so no new cross-owner
  import edge is created despite both files touching the same numeric
  value.
- New / changed coupling, cycles, or shared state: reduces
  duplication WITHIN `persistence.py` (one constant instead of two
  independent literals for the two DB-write sites); `reviewer.py`'s
  cap is renamed in place with no coupling change.
- `persistence.py` is currently unowned by any spec's `owns.code` and
  is claimed here; `reviewer.py` remains owned by
  `SP-RETRY-BACKOFF-DEDUP` (`depends_on`), and this spec touches only
  `_format_existing_files`, which that spec's own scope
  (`_call_with_retry` extraction) does not touch.

## Diagram

N/A (in-place literal-to-constant refactor, no new modules or edges).

## Acceptance Criteria

- [ ] AC1: unit — `persistence._PERSISTED_JSON_TRUNCATE_CHARS` exists,
  equals `32000`, and both `record_dialogue` and
  `record_coder_response` truncate at exactly that length (verified by
  round-tripping a payload/fixes list one character over and one
  character at the cap through a real `init_schema` + insert +
  `fetch_dialogue`/direct row read, asserting the persisted string
  length). This test FAILS on pre-change code with an `ImportError`
  (no such name exists in `persistence.py` today).
- [ ] AC2: unit — `reviewer._EXISTING_FILE_PROMPT_TRUNCATE_CHARS`
  exists, equals `32000`, and is a DIFFERENT symbol object from
  `persistence._PERSISTED_JSON_TRUNCATE_CHARS` (`is not`) — i.e.
  `reviewer.py` does not import `persistence.py`'s constant. This test
  FAILS on pre-change code with an `ImportError` (no such name exists
  in `reviewer.py` today).
- [ ] AC3: unit — `_format_existing_files` truncates a file's contents
  at exactly `_EXISTING_FILE_PROMPT_TRUNCATE_CHARS` characters and
  appends the truncation marker, matching current behavior
  byte-for-byte (regression guard against G4/NG1 drift).
- [ ] AC4: promised tests — new file
  `tests/unit/test_truncate_cap_named_const.py`:
  `test_persisted_json_truncate_chars_constant_backs_dialogue_write`,
  `test_persisted_json_truncate_chars_constant_backs_coder_response_write`,
  `test_existing_file_prompt_truncate_chars_is_a_distinct_constant`,
  `test_format_existing_files_truncates_at_named_constant_length`.
- [ ] AC5: no import of `repoach.review.persistence` appears anywhere
  in `src/repoach/review/reviewer.py` (grep-verified in the test or a
  static assertion) — the explicit G4/NG4 no-coupling guarantee.
- [ ] AC6: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `repoach arch graph --check` (or the repo's current architecture
  gate command) exits 0.

## Open Questions

(none)
