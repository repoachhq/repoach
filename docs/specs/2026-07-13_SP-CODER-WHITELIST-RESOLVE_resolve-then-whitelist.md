---
id: SP-CODER-WHITELIST-RESOLVE
title: Re-run the write whitelist on the resolved repo-relative path
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

# Re-run the write whitelist on the resolved repo-relative path

## Intent

Close the Coder write-path whitelist bypass: `is_path_allowed`
matches on the RAW string, so a `./`-, `//`- or symlink-disguised
path reaches a forbidden target (`.github/workflows/*`,
`.githooks/*`, `.git/*`, `prompts/review/*`, `.env*`) that the
allow-list is meant to protect. Enforce the whitelist on the
RESOLVED repo-relative form, mirroring the already-hardened Developer
write path.

## Context

`is_path_allowed` (`src/ferova/review/coder_loop.py:101-114`) splits
the raw string on `/`, rejects `..` and absolute paths, then tests
`norm.startswith(prefix)` against `FORBIDDEN_PREFIXES`
(`coder_loop.py:66`) on the RAW string only. Verified by execution:
`./.github/workflows/ci.yml`, `.//.githooks/pre-commit`,
`./.git/hooks/pre-commit`, and `./prompts/review/x.md` all return
`True` — the `./` / `//` prefix means the string never `startswith`
the bare forbidden prefix, so the guard passes.

`apply_fixes` (`coder_loop.py:475-531`) calls `is_path_allowed`
on the raw path (`coder_loop.py:485`), then resolves
`(repo_root / path_raw).resolve()` and re-checks ONLY repo
containment via `target.relative_to(repo_root)`
(`coder_loop.py:520-524`) — it never re-runs the whitelist on the
resolved repo-relative form. An in-repo symlink whose name is
allow-listed but whose resolved target is forbidden bypasses
identically (containment holds; whitelist is never re-applied).

The Developer write path already does this correctly:
`_resolve_writable` (`src/ferova/review/devagent_tools.py:106-130`)
jails to the repo root, computes `resolved.relative_to(repo_root)`,
and enforces `is_path_allowed(relative)` on THAT normalized form plus
the step's file contract. This spec brings `apply_fixes` up to the
same standard.

Audit 2026-07-13 finding C1 (CRITICAL, verified by execution).
Execution: hand-implement with human review (audit 2026-07-13) —
merge-path change.

## Goals

- G1: every write in `apply_fixes` is authorised by
  `is_path_allowed` evaluated on the RESOLVED repo-relative path, not
  the raw string.
- G2: a `./`-, `//`- or otherwise dot-normalised forbidden path is
  rejected and recorded in the `rejected` list.
- G3: an in-repo symlink whose resolved target is forbidden (or
  escapes the repo) is rejected, never written through.

## Non-Goals

- NG1: no change to `FORBIDDEN_PATHS` / `FORBIDDEN_PREFIXES` contents.
- NG2: no change to the Developer path — `_resolve_writable` already
  enforces the resolved-form whitelist; this only aligns the Coder.
- NG3: no change to `is_path_allowed`'s own signature or the raw
  pre-checks (they stay as a cheap first filter).

## Assumptions

- A1: `repo_root` is already resolved in `apply_fixes`
  (`coder_loop.py:477`), so `resolved.relative_to(repo_root)` yields
  the canonical repo-relative posix path to feed the whitelist.
- A2: the same resolve-then-whitelist ordering used by
  `_resolve_writable` is the sanctioned pattern and should be reused
  (extract a shared helper or replicate its exact logic).

## Interface

N/A (in-place fix in `apply_fixes`; the raw-string `is_path_allowed`
signature is unchanged — it gains a resolved-form caller, not a new
parameter).

## Behavior

### Nominal

For each fix: after the existing raw pre-check and after resolving
`target = (repo_root / path_raw).resolve()`, compute
`relative = target.relative_to(repo_root).as_posix()` and require
`is_path_allowed(relative)` to hold before any write. An allowed
in-repo file resolves to a permitted relative path and is written as
today.

### Edge cases

- `./.github/workflows/ci.yml`, `.//.githooks/pre-commit`,
  `./.git/hooks/pre-commit`, `./prompts/review/x.md` → resolved
  relative form is `.github/workflows/ci.yml` etc., which
  `is_path_allowed` rejects → recorded in `rejected`, not written.
- In-repo symlink named `docs/note.md` pointing at
  `.github/workflows/ci.yml` → resolved target is the forbidden path
  → rejected.

### Failure scenarios

- Symlink or `..` chain that resolves OUTSIDE the repo → the existing
  `relative_to` containment `ValueError` path fires first
  (`coder_path_escapes_repo`), rejected. Fail CLOSED: any path whose
  resolved form is not both inside the repo AND whitelist-allowed is
  rejected and appended to `rejected`; the file is never written.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `coder_loop.py` (owned by an existing spec); introduces no new
  cross-owner import. May reuse the resolve-then-whitelist logic from
  `devagent_tools._resolve_writable` (same module tree).
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — `is_path_allowed` still rejects the bare forbidden
  prefixes, and the new resolved-form check in `apply_fixes` rejects
  `./.github/workflows/x.yml`, `.//.githooks/pre-commit`,
  `./.git/hooks/pre-commit`, `./prompts/review/x.md`.
- [ ] AC2 (INTEGRATION): drive `apply_fixes` in a tmp git repo (real
  filesystem, real `resolve()`) with a fixes list containing a fix
  whose `path` is `./.github/workflows/x.yml` and one whose path is
  `.//.githooks/pre-commit`; assert neither file is written to disk
  AND both raw paths appear in the returned `rejected` list. Add a
  case with an in-repo symlink resolving to a forbidden target and
  assert it is rejected too.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_coder_loop.py::test_apply_fixes_rejects_dot_normalised_forbidden_paths`
  and `::test_apply_fixes_rejects_symlink_to_forbidden_target`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit`
  green; zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no
  `# noqa`; `ferova arch graph --check` exits 0.

## Open Questions

OQ1: implement by hand + human review before re-trusting auto-merge
(audit) — this is the primary write-path guard for bot-authored
changes.
