---
id: SP-DB-PATH-XDG
title: Anchor the default REPOACH_DB_PATH to the repo root instead of raw CWD
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: [src/repoach/core/config.py, tests/unit/test_config.py]
  resources: []

depends_on: [SP-CONFIG-ENV-ANCHOR]
provides_to: []

constraints: {}
---

# Anchor the default REPOACH_DB_PATH to the repo root instead of raw CWD

## Intent

`Settings.db_path` still defaults to the relative `Path("./data/repoach.db")`
(`src/repoach/core/config.py:138-141`), so any invocation of the `repoach`
CLI from a directory other than the repo root — a cron job, an operator's
shell sitting in a different `cwd`, a script that `cd`s elsewhere first —
silently creates or reads a *different* `./data/repoach.db` with no error,
splitting review-team ledger state across multiple files and producing
confusing "missing findings" symptoms. Anchor the relative default (and any
relative operator override) against the same repo-root anchor
`_repo_root()` already computes for env-file loading, so `db_path` resolves
identically regardless of the process's current working directory.

## Context

- `docs/tech_debt.md` item 18 documents this gap (originally raised against
  a since-renamed `config.py:71-72`; the field has moved but not changed
  shape).
- Confirmed still true on `develop` (`src/repoach/core/config.py:138-141`):
  ```python
  db_path: Path = Field(
      default=Path("./data/repoach.db"),
      description="Path to the main SQLite database file (review-team persistence).",
  )
  ```
  This `Path` is never resolved against anything at construction time — it
  stays exactly `./data/repoach.db`, a value whose meaning depends entirely
  on the CWD of whichever process later calls `.parent.mkdir()`
  (`Settings.ensure_dirs`, `config.py:315-320`) or opens
  `f"sqlite:///{db_path}"` (e.g. `cli/review_cmds.py:590-591`).
- `.github/workflows/auto-review.yml` (lines 210, 419, 494, 662) already
  re-points `REPOACH_DB_PATH` to an absolute `${{ runner.temp }}/...` path
  per job, so CI is unaffected either way — this gap only bites local /
  manual / cron invocations.
- `SP-CONFIG-ENV-ANCHOR` (`src/repoach/core/config.py:30-52`) already solved
  the identical CWD-dependence problem for `.env`/`chains.env` loading by
  introducing `_repo_root()` — a module-level, monkeypatch-friendly helper
  that walks up from `config.py`'s own file to the nearest ancestor
  containing `pyproject.toml`. That spec's frontmatter leaves `owns.code`
  empty and never claims `db_path`, so this file is not exclusively owned
  by any existing spec; this spec reuses `_repo_root()` as-is rather than
  introducing a second, competing anchor mechanism (e.g. `platformdirs`).

## Goals

- G1: When `db_path` resolves to a relative `Path` — whether the built-in
  `./data/repoach.db` default or an operator-supplied relative
  `REPOACH_DB_PATH` — `Settings` anchors it against `_repo_root()` (the
  same anchor `_anchored_env_files` uses), not the process's raw CWD.
- G2: The anchored value is an absolute, resolved `Path` by the time
  `Settings()` finishes constructing, so every consumer reading
  `get_settings().db_path` (`ensure_dirs`, `cli/main.py`,
  `cli/review_cmds.py`, `cli/release_cmds.py`, `cli/chain_status.py`,
  `health/store.py`) sees the same file regardless of its own CWD.
- G3: An explicit absolute `REPOACH_DB_PATH` (the CI shape, and any
  operator override) passes through unchanged — no behavior change for the
  already-correct case.

## Non-Goals

- NG1: No adoption of `platformdirs` / an XDG user-data directory
  (`~/.local/share/repoach/`) — that is a separate design decision about
  *where* the DB should live long-term; this spec only fixes the
  CWD-ambiguity by anchoring the existing repo-relative default to a
  stable root, matching the precedent `_repo_root()` already set for env
  files. No new third-party dependency.
- NG2: No behavior change for any caller that already sets `REPOACH_DB_PATH`
  to an absolute path (CI's `runner.temp` path, and any operator who
  already does this) — G3 keeps that path byte-for-byte identical.
- NG3: No change to `_repo_root()` or `_anchored_env_files` themselves
  (`config.py:30-72`) — this spec is additive, reusing them unmodified.
- NG4: No change to `Settings.ensure_dirs()`'s directory-creation logic
  beyond it now receiving an already-anchored `db_path` — its
  `mkdir(parents=True, exist_ok=True)` call is untouched.
- NG5: No migration of any existing `./data/repoach.db` file already on
  disk from a prior CWD-relative run — purely a forward-looking resolution
  fix.

## Interface

`src/repoach/core/config.py`, class `Settings`:

- New `model_validator(mode="after")` method, e.g.
  `_anchor_relative_db_path`, added alongside the existing
  `require_llm_proxy_base_url` / `require_proxy_token_in_prod`
  after-validators:

  ```python
  @model_validator(mode="after")
  def _anchor_relative_db_path(self) -> Settings:
      if not self.db_path.is_absolute():
          self.db_path = (_repo_root() / self.db_path).resolve()
      return self
  ```

- No change to the field declaration itself
  (`db_path: Path = Field(default=Path("./data/repoach.db"), ...)`); the
  validator runs after field population, mirroring how
  `require_llm_proxy_base_url` runs after `llm_proxy_base_url` resolves.

## Behavior

### Nominal

- No `REPOACH_DB_PATH` set, process started from any CWD → `db_path`
  resolves to `<repo_root>/data/repoach.db` (absolute), identical
  regardless of the invoking CWD.
- `REPOACH_DB_PATH=/absolute/path/to.db` set (the CI shape) → `db_path`
  is that exact absolute path, untouched by the anchor.

### Edge cases

- `REPOACH_DB_PATH=relative/override.db` set (a relative operator
  override) → anchored the same way as the default:
  `<repo_root>/relative/override.db`, not `<cwd>/relative/override.db`.
- `_repo_root()` falls back to the installed package's own top-level
  directory when no `pyproject.toml` is found above it (existing
  documented fallback in `_repo_root`'s docstring) — `db_path` anchors to
  that same fallback root, consistent with where `.env`/`chains.env`
  would have anchored.

### Failure scenarios

- None new — this validator cannot raise; a relative `db_path` always has
  some anchor root to resolve against (`_repo_root()` always returns a
  path, per its own fallback), so there is no unresolved state to fail
  loud on (unlike `llm_proxy_base_url`, which has no safe default).

## Acceptance Criteria

- [ ] AC1: unit — from a foreign `tmp_path` CWD (`monkeypatch.chdir`), with
  `config._repo_root` monkeypatched to a distinct anchored fixture
  directory and no `REPOACH_DB_PATH` set, `Settings().db_path` equals
  `(anchored_root / "data" / "repoach.db").resolve()` — proving it did
  NOT resolve against the foreign CWD.
- [ ] AC2: unit — with `REPOACH_DB_PATH` set to an absolute path (any
  anchored root, any foreign CWD), `Settings().db_path` equals that exact
  absolute path, unchanged (regression guard for G3 / NG2).
- [ ] AC3: promised tests, added to the existing
  `tests/unit/test_config.py` —
  `test_db_path_anchored_from_foreign_cwd` and
  `test_db_path_absolute_env_override_untouched`. Both selectors must
  FAIL against pre-change code: pre-fix, `db_path` stays the bare
  `Path("./data/repoach.db")` (or the bare relative override) with no
  anchoring validator, so `test_db_path_anchored_from_foreign_cwd`'s
  assertion (`== anchored_root / "data" / "repoach.db"`) fails because the
  pre-fix value never resolves to an absolute path at all.
- [ ] AC4: `ruff check` + `ruff format --check` + `pytest tests/unit` all
  green; zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `repoach arch graph --check` (or the current equivalent) exits 0.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `core/config.py`, reusing the existing `_repo_root()` primitive
  (`SP-CONFIG-ENV-ANCHOR`); no new import, no new third-party package.
- New / changed coupling, cycles, or shared state: none — `db_path`
  resolution becomes consistent with the env-file anchor already used in
  the same module; no new cross-module coupling.

## Diagram

N/A (in-place fix, single field's post-construction resolution).

## Open Questions

(none)
