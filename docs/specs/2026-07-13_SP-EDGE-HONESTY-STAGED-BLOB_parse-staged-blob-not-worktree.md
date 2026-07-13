---
id: SP-EDGE-HONESTY-STAGED-BLOB
title: Read the staged blob (not the worktree) in edge-honesty staged mode
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

# Read the staged blob (not the worktree) in edge-honesty staged mode

## Intent

The edge-honesty gate's staged (pre-commit) mode lists staged files via
`git diff --cached` but then reads the WORKTREE copy of each file, so a
file staged with an undeclared cross-spec import can pass the hook after
its worktree copy is reverted — and the commit still carries the
undeclared edge. Read the staged blob, so the gate checks exactly what
will be committed.

## Context

Audit 2026-07-13 finding M17 plus two edge_honesty lows.

- `src/ferova/lint/edge_honesty.py:140-148` (`_check_file`): staged
  mode collects file names from `git diff --cached ...`
  (`gather_changed_files`, `edge_honesty.py:238-243`) but the body
  parses `(repo_root / rel_path).read_text(...)` — the worktree copy,
  not the staged content. Stage a file with an undeclared import, then
  revert the worktree copy: `git diff --cached` still lists it,
  `_check_file` reads the clean worktree, the hook passes, and the
  commit carries the undeclared edge.
- Same worktree-read bug in the spec-presence path:
  `check_added_specs` (`edge_honesty.py:303,320`) reads
  `(repo_root / rel_path).read_text(...)`.
- Low — `gather_changed_files:241` and `gather_added_specs:288`
  interpolate `base` into the git argv without a `--` separator
  (`f"{base}...HEAD"`); a `base` value beginning with `-` is parsed by
  git as an option, not a ref.
- Low — `load_frontier_suppress:171-190` reads a
  `[tool.ferova.arch] frontier_suppress` table that is ABSENT from
  `pyproject.toml`, so it always returns an empty set (dead
  configuration surface).

`edge_honesty.py` is owned by an existing arch/lint spec; this is an
in-place modification.

## Goals

- G1: in staged mode, `_check_file` and `check_added_specs` read the
  STAGED BLOB (`git show :<path>`), not the worktree copy, so the gate
  validates exactly the content that will be committed.
- G2: worktree mode is unchanged (reads the worktree, correct there).
- G3: git ref ranges are passed after a `--` separator so a
  `-`-leading `base` cannot be reinterpreted as a git option.
- G4: the dead `frontier_suppress` surface is resolved — either wired
  to a real `[tool.ferova.arch]` table declared in `pyproject.toml`, or
  removed so the code carries no configuration that silently does
  nothing.

## Non-Goals

- NG1: no change to the edge-detection logic itself
  (`_intra_repo_imports`, `_import_owner`, `_table_literals`) — only
  the SOURCE of the bytes it parses.
- NG2: no new CLI surface; the `ferova arch` entrypoints are unchanged.
- NG3: no change to the `_TEMPLATE_ERA` grandfathering rule.

## Assumptions

- A1: `git show :<path>` returns the staged blob for a tracked,
  staged path and exits non-zero for an unstaged path; the gate only
  calls it for paths that `--diff-filter=ACMR --cached` already listed.
- A2: a renamed staged file (`R`) is addressed by its new path in the
  `--cached` listing, for which `git show :<newpath>` is valid.

## Interface

`src/ferova/lint/edge_honesty.py`:
- Add a helper
  `_read_source(rel_path: str, *, staged: bool, repo_root: Path) -> str`
  — returns `git show :<rel_path>` output when `staged`, else
  `(repo_root / rel_path).read_text(...)`.
- Thread the `staged` flag into `_check_file` and `check_added_specs`
  (and `check_diff`/`run` which already carry it) so both read via
  `_read_source`.
- In `gather_changed_files:241` and `gather_added_specs:288`, insert
  `"--"` before the ref range:
  `["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD", "--"]`
  (and analogously for the added-specs command).
- Resolve `load_frontier_suppress` per G4 (wire the table into
  `pyproject.toml` under `[tool.ferova.arch]`, or delete the reader and
  its `suppress=` plumbing).

## Behavior

### Nominal

- Staged mode: each listed path is parsed from its staged blob;
  undeclared imports present in the staged content are flagged.

### Edge cases

- File staged WITH an undeclared edge but reverted in the worktree →
  FLAGGED in staged mode (the core fix), where today it passes.
- File clean in the stage but dirtied in the worktree with an
  undeclared edge → not flagged by staged mode (correct: it is not
  being committed); worktree/diff mode still catches it against `base`.
- `base` value beginning with `-` → treated as a ref after `--`, not a
  git option (no silent misparse).

### Failure scenarios

- `git show :<path>` fails for a listed staged path → surface the
  `CalledProcessError` loudly (consistent with the existing
  `check=True` contract at `edge_honesty.py:242,289`); the gate fails
  rather than silently skipping the file.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `edge_honesty.py` (owned by an existing arch/lint spec); introduces
  no new cross-owner import. If G4 wires the table, it adds a
  `[tool.ferova.arch]` section to `pyproject.toml` (config, not code
  ownership).
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place fix).

## Acceptance Criteria

- [ ] AC1: unit — `_read_source(..., staged=True)` returns the staged
  blob and `_read_source(..., staged=False)` returns the worktree copy;
  the two git-argv builders include a `--` separator.
- [ ] AC2 (INTEGRATION): in a tmp git repo (truthful boundary fake for
  git — real `git init`, real commits, real staging), stage a governed
  file with an undeclared cross-spec import, then overwrite the
  worktree copy to remove the import; run the staged-mode gate
  (`run(base=..., staged=True, ...)`) and assert the undeclared edge is
  FLAGGED — driving the real staged-mode entrypoint, not a helper in
  isolation. A second case asserts worktree mode is unaffected.
- [ ] AC3: promised tests —
  `tests/unit/test_edge_honesty.py::test_staged_mode_reads_staged_blob_not_worktree`,
  `::test_staged_reverted_worktree_still_flagged`,
  `::test_git_argv_has_double_dash_separator`, and (per G4)
  `::test_frontier_suppress_wired` or `::test_frontier_suppress_removed`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
