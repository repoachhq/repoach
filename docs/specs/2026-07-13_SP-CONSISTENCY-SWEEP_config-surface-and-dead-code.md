---
id: SP-CONSISTENCY-SWEEP
title: Sweep dead attributes, ad-hoc env reads, loose spec-glob, and substring exit codes
version: 0.1
status: draft
author: jfaye (Ferova audit 2026-07-13)
created: 2026-07-13
updated: 2026-07-13

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Sweep dead attributes, ad-hoc env reads, loose spec-glob, and substring exit codes

## Intent

Retire four small independent consistency/dead-code LOW findings that
each widen a surface or invite a silent mismatch: two dead attributes
that re-pin a raw token on a long-lived object, two env vars read
outside Settings, a spec-file glob that can load a sibling spec, and a
CLI exit-code map built on free-text substring matching. Each is an
in-place modification of an already-owned file; the bundle keeps them
together because they are individually too small for their own spec.

## Context

Audit 2026-07-13 findings:

- **C1 (dead attrs)** — `src/ferova/agent_engine/agent_loop.py:344-345`
  assigns `self._base_url = f"{base_url}/v1"` and
  `self._api_key = api_key`. Grep-verified: neither is ever read
  (`_base_url` appears only at its assignment and the local `base_url`
  at line 332; `self._api_key` only at its assignment). They are dead,
  and `self._api_key` pins the raw token on a second long-lived object,
  defeating the `SecretStr` hygiene the rest of the path maintains.
  Remove both.
- **C2 (ad-hoc env)** — `src/ferova/review/planner.py:61` reads
  `FEROVA_PLANNER_PARSE_ATTEMPTS` and
  `src/ferova/review/coder_loop.py:573` reads `FEROVA_CODER_PYTHONS`,
  both via raw `os.environ`, bypassing `core/config.py` `Settings`
  (`src/ferova/core/config.py:30`) and both undocumented in
  `.env.example`. Move both to `Settings` (with `FEROVA_*` aliases,
  preserving the existing defaults: parse-attempts default 5 and
  clamp-to-≥1; `FEROVA_CODER_PYTHONS` a comma list, unset → `[None]`)
  and document them in `.env.example`.
- **C3 (loose glob)** — `src/ferova/review/spec.py:224-230`
  `_resolve_plan_file` uses `base.glob(f"*{spec_id}*.md")`, so
  `spec_id="SP-SEC"` also matches a sibling like
  `..._SP-SECURITY-FOO_....md` and the lexicographically-last match
  wins — silently loading the wrong spec. Tighten to the exact
  `_(SP-…)_` boundary extraction already used by `_scan_known_spec_ids`
  (`src/ferova/review/spec.py:111`, pattern `_(SP-[A-Z0-9-]+)_`): match
  a file only when the extracted id equals the requested id.
- **C4 (substring exit codes)** —
  `src/ferova/cli/review_cmds.py:487-497` maps `typer.Exit` codes by
  substring-matching the free-text `result.no_op_reason`
  (`"ruff" in reason`, `"pytest" in reason`, `"spec not found"`,
  `"no fixes"`, `"self-verify"`, `"decompose"`/`"supersede"`). The same
  brittleness appears at `src/ferova/cli/review_cmds.py:559-563` for the
  planner outcome. The reasons originate as free text in
  `src/ferova/review/coder_findings.py:464-632` (e.g. line 603
  `"ruff gate red; ..."`, line 615 `"pytest red; ..."`). Replace the
  substring matching with a structured reason-code enum carried on the
  result, set at each `no_op_reason=` site, and mapped to exit codes by
  identity.

Each fix is an in-place modification of an existing already-owned file
(`owns.code: []`, `depends_on: []`). No file is newly owned.

## Goals

- G1: `AgentLoop` no longer assigns the dead `_base_url` / `_api_key`
  attributes; no raw token is pinned on that object.
- G2: `FEROVA_PLANNER_PARSE_ATTEMPTS` and `FEROVA_CODER_PYTHONS` are
  read through `Settings`, injectable in tests, and documented in
  `.env.example`, with defaults and clamping unchanged.
- G3: `_resolve_plan_file` resolves only files whose exact extracted
  `SP-…` id equals the requested id — a sibling spec is never loaded.
- G4: CLI exit codes derive from a structured reason-code field, not
  from substring matching free text; the current code numbers are
  preserved.

## Non-Goals

- NG1: no change to the observable exit-code NUMBERS (5/4/6/7/3/1 for
  `review fix`; 5/2/1 for `plan`) — only the mechanism that selects
  them changes.
- NG2: no change to the human-readable `no_op_reason` strings that reach
  logs/PR comments — the enum is ADDED alongside them, not a
  replacement of the text.
- NG3: no generalization of the credits/env surface beyond the two vars
  named (other ad-hoc reads, if any, are out of scope).
- NG4: no change to planner/coder default values or clamping behavior.

## Assumptions

- A1: `_scan_known_spec_ids`'s `_(SP-[A-Z0-9-]+)_` extraction
  (`spec.py:111`) is the canonical id-in-filename form; every governed
  spec filename embeds `_<SP-ID>_` (verified against the current
  `docs/specs/` corpus).
- A2: the coder/planner result objects
  (`src/ferova/review/coder_findings.py`) can carry an additional
  enum field without breaking their existing consumers (the field is
  additive, defaulting to a neutral code).
- A3: moving the two env vars into `Settings` preserves precedence
  (`chains.env` then `.env`, `FEROVA_` prefix, case-insensitive —
  `core/config.py:38-44`); tests inject via the settings object rather
  than `os.environ`.

## Interface

- `AgentLoop.__init__` (`agent_engine/agent_loop.py`) — the two dead
  assignments removed; signature unchanged.
- `Settings` (`core/config.py`) — two new fields:
  `planner_parse_attempts: int = 5` (clamped to ≥1 at the read site, as
  today) and `coder_pythons: str = ""` (raw comma list; parsed to
  `list[str | None]` by the existing helper, now reading the setting).
  `planner.py` and `coder_loop.py` read these from a `Settings`
  instance instead of `os.environ`; their existing parse/clamp helpers
  keep the same return contract.
- `_resolve_plan_file(spec_id, *, root=None) -> Path | None`
  (`review/spec.py`) — signature unchanged; internals switch from
  `glob(f"*{spec_id}*.md")` to enumerating `*.md`, extracting the id
  via the shared `_(SP-[A-Z0-9-]+)_` pattern, and keeping only files
  whose extracted id equals `spec_id`; ties still resolved by
  lexicographic-last (most recent), as documented.
- New `ReasonCode` enum (module-local to `coder_findings.py`, or a small
  sibling constant) with members covering the mapped cases
  (`SPEC_NOT_FOUND`, `NO_FIXES`, `WHITELIST_REJECTED`, `SELF_VERIFY`,
  `DECOMPOSE`/`SUPERSEDE`, `RUFF_RED`, `PYTEST_RED`, `PUSH_FAILED`,
  `NONE`). The result object gains `reason_code: ReasonCode =
  ReasonCode.NONE`. `review_cmds.py` maps `reason_code` → exit code by
  identity.

Errors: none new; exit codes unchanged.

## Behavior

### Nominal

- C1: constructing an `AgentLoop` no longer sets `_base_url`/`_api_key`;
  behavior of `run_oneshot` and the proxy client (built from the local
  `base_url` at line 332) is identical.
- C2: with the env vars unset, planner uses 5 parse attempts and
  `coder_pythons` yields `[None]` — identical to today; a set value is
  honored through `Settings`.
- C3: `_resolve_plan_file("SP-SEC")` returns the `SP-SEC` spec (or
  `None`), never a `SP-SECURITY-*` sibling.
- C4: each `review fix` / `plan` outcome maps to the same exit code as
  today, now selected by `reason_code`.

### Edge cases

- C2: an out-of-range `FEROVA_PLANNER_PARSE_ATTEMPTS` (0, negative,
  non-int) still clamps to 1 / falls back to the default via the
  existing helper.
- C3: a filename with no `_SP-…_` segment is skipped (never a false
  match); multiple exact-id matches → lexicographic-last, as before.
- C4: a `no_op_reason` that does not correspond to any mapped code
  carries `ReasonCode.NONE` → the existing default exit path
  (`Exit(1)` when not pushed) is preserved.

### Failure scenarios

- C3 is the fail-open finding here: today a mistyped/ambiguous id
  silently loads a sibling spec's plan. The fix fails CLOSED — an id
  that matches no exact-id file returns `None` (surfaced as the
  existing `FileNotFoundError` in `load_spec`), never a wrong spec.

## Architecture Impact

- Adds/Removes dependency: none — in-place modifications of
  `agent_engine/agent_loop.py`, `core/config.py`, `review/planner.py`,
  `review/coder_loop.py`, `review/spec.py`, `review/coder_findings.py`,
  and `cli/review_cmds.py` (each owned by an existing spec), plus
  `.env.example` (a resource doc). Introduces no new cross-owner import;
  planner/coder already import `Settings`.
- New / changed coupling, cycles, or shared state: the `ReasonCode`
  enum couples `coder_findings.py` (producer) and `review_cmds.py`
  (consumer) via a shared enum in the review module — a tightening of
  an already-present textual coupling, not a new module boundary.

## Diagram

N/A (four independent in-place fixes).

## Acceptance Criteria

- [ ] AC1 (C1 unit): constructing an `AgentLoop` does not set
  `_base_url` / `_api_key` (assert the attributes are absent) and
  `run_oneshot` still builds its client from the resolved base URL.
- [ ] AC2 (C2 unit): with `Settings` overridden, planner reads its
  parse-attempts through the setting (not `os.environ`), and
  `coder_pythons` parsing yields `[None]` when unset and the parsed
  list when set; clamping to ≥1 preserved.
- [ ] AC3 (C3 unit): in a tmp specs dir containing both
  `..._SP-SEC_...md` and `..._SP-SECURITY-FOO_...md`,
  `_resolve_plan_file("SP-SEC", root=tmp)` returns the `SP-SEC` file,
  and `_resolve_plan_file("SP-MISSING", root=tmp)` returns `None`.
- [ ] AC4 (C4 unit): each `no_op_reason=` site in `coder_findings.py`
  sets the expected `ReasonCode`, and the `review_cmds.py` code map
  turns each `ReasonCode` into its documented exit number.
- [ ] AC5 (INTEGRATION — at least one real end-to-end assertion):
  (a) drive `load_spec("SP-SEC", root=tmp)` through the real
  `_resolve_plan_file` in a tmp specs dir seeded with a sibling
  `SP-SECURITY-*` spec (truthful boundary fake: real files on disk),
  asserting the `SP-SEC` document is loaded and the sibling is not; AND
  (b) drive `review fix` via `CliRunner` with the coder result injected
  to carry a `RUFF_RED` reason code, asserting exit code 3 through the
  real CLI command (reason-code path, not substring). At least one of
  these MUST exercise the actual entrypoint, not a helper in isolation.
- [ ] AC6: promised test files + selectors —
  `tests/unit/test_agent_loop_no_dead_attrs.py::test_base_url_and_api_key_not_pinned`;
  `tests/unit/test_planner_settings_knob.py::test_parse_attempts_via_settings`;
  `tests/unit/test_review_spec_resolve.py::test_exact_id_beats_sibling`;
  `tests/unit/test_review_cmds_exit_codes.py::test_reason_code_maps_to_exit`.
- [ ] AC7: `.env.example` documents `FEROVA_PLANNER_PARSE_ATTEMPTS` and
  `FEROVA_CODER_PYTHONS`.
- [ ] AC8: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

- OQ1: capacity — this bundle touches seven source files plus
  `.env.example`. Each edit is small and independent, so it stays within
  Developer capacity as a single spec, but if the plan review judges the
  fan-out too wide for one `ferova develop` session, a clean split is:
  SP-CONSISTENCY-SWEEP-A = C1+C2 (agent-loop dead attrs + env→Settings),
  SP-CONSISTENCY-SWEEP-B = C3+C4 (spec-glob tightening + reason-code exit
  map). The two halves share no file and can land in either order.
