---
id: SP-DEV-STEP-LOOP-HARDEN
title: Harden the dev_runner step loop — scoped revert, lint-aware retry, inline-comment auto-heal
version: 0.1
status: draft
author: agent
created: 2026-06-25
updated: 2026-06-25

owns:
  code: src/repoach/review/inline_comment_heal.py
  resources: N/A

depends_on: []                    # new leaf imports the frontier lint scanner; amends frontier coder_loop.py / dev_runner.py

provides_to: []                  # AUTO-maintained
constraints: {}
---

# SP-DEV-STEP-LOOP-HARDEN — make the autonomous Developer survive small leaves

## Intent
Three targeted fixes to the `ferova develop` per-step loop, each closing a
failure class observed live while dispatching a small additive leaf
(SP-CHAINPILOT-CODER-OUTCOMES, 2026-06-24). Together they stop the loop from
**destroying its own inputs** and from **failing a near-correct step** on a
mechanical lint slip.

## Context
The 2b dispatch failed step 1 after both attempts and the revert wiped the
untracked spec. Autopsy found three independent issues, none of which is the
historical anchored-edit fragility (the leaf was a *new* file → full-file
create, not an anchored edit):

1. **`revert_working_tree` destroys untracked inputs.** It runs
   `git reset --hard HEAD` **then `git clean -fd`**; the unscoped `clean -fd`
   removes every untracked file, including the spec in `docs/specs/` (not
   gitignored). The revert is meant to discard the *step's code*, not the
   session's doc inputs.
2. **The dominant small-new-file failure is the no-inline-comments golden
   rule.** A weak coder tail (deepseek-v4-flash) emitted `# ...` trailing
   comments, tripping the repo lint gate (`run_repo_lint_gates`). The single
   retry was not enough to make it comply.
3. **Only one retry.** `run_step` iterates `for attempt in (1, 2)`; a step
   that is one nudge from green on a lint-class gate has no third chance.

## Goals
- G1 (scoped revert): `revert_working_tree`
  (`src/ferova/review/coder_loop.py`) scopes its `git clean -fd` to the
  code roots the Developer/Coder write (`src`, `tests`, `scripts`, those that
  exist), so untracked files under `docs/` (the spec) survive a failed-step
  revert. The `reset --hard HEAD` is unchanged.
- G2 (inline-comment auto-heal): a new leaf
  `src/ferova/review/inline_comment_heal.py` exposing
  `heal_inline_comments(repo_root, paths) -> list[str]`, which reuses
  `ferova.lint.no_inline_comments.scan_file` to find `kind == "inline"`
  violations in the given `.py` files and deterministically strips each from
  the `#` column to end-of-line (preserving the EOL and the code to its left).
  Returns the relative paths it healed.
- G3 (heal hook): `run_step` (`dev_runner.py`) calls `heal_inline_comments` on
  the step's contract paths immediately after a successful `apply_fixes` and
  before the gate chain, logging the healed paths — so a golden-rule slip is
  auto-fixed rather than failing the repo lint gate.
- G4 (lint-aware third attempt): `run_step` allows a third attempt **only when
  the prior attempt failed a lint-class gate** (ruff or repo-lint); non-lint
  failures still stop after two attempts (no extra latency for genuinely broken
  steps).

## Non-Goals
- NG1: Does NOT change the anchored-edit / full-file decision for small
  *existing* files (a `prompts/review/` persona change, hand-shipped
  separately).
- NG2: Does NOT heal `noqa` violations or standalone full-line comments — only
  inline trailing comments (removing whole lines is unsafe to automate).
- NG3: Does NOT touch the Coder loop's own retry budget or any gate threshold
  beyond the step loop described here.
- NG4: Does NOT alter `reset --hard HEAD`; tracked changes still revert fully.

## Assumptions
- A1: The Developer/Coder write only under `src` / `tests` / `scripts` (path
  whitelist + repo layout), so scoping the clean there fully reverts a step's
  code while preserving doc inputs.
- A2: `scan_file` reports the exact 1-based column of each inline `#` via
  `tokenize`, so a comment inside a string is never mis-detected and the cut is
  safe.
- A3: Healing removes only comment text, never code, so a healed file stays
  syntactically valid for the downstream syntax/ruff gates.

## Interface
New:
- `src/ferova/review/inline_comment_heal.py` —
  `heal_inline_comments(repo_root: Path, paths: Iterable[str]) -> list[str]`.

Changed (frontier):
- `coder_loop.py` — `revert_working_tree` scopes the clean.
- `dev_runner.py` — `run_step` heals after apply, and grants a lint-class third
  attempt.

## Behavior

### Nominal
- A step whose generated test file carries `result = f()  # explain` →
  `heal_inline_comments` strips the comment in place → repo lint gate passes →
  step commits on attempt 1.
- A step that fails ruff on attempt 2 → a third attempt runs.

### Edge cases
- A failed step with an untracked spec in `docs/specs/` → after revert the spec
  is still on disk.
- A step that fails the import gate twice → no third attempt (non-lint).
- A `.py` file with no inline violations → `heal_inline_comments` leaves it
  byte-identical and does not list it.
- A non-`.py` contract path or a missing file → skipped.

### Failure scenarios
- A file that fails to tokenise → `scan_file` returns no violations → heal is a
  no-op for it (the syntax gate then reports the real error).

## Architecture Impact
- New leaf imports the frontier `ferova.lint.no_inline_comments`; no new
  cross-`owns` edge, `arch check` stays green.
- `coder_loop.py` / `dev_runner.py` are frontier; their amendments are
  additive and non-blocking under the edge-honesty gate.
- No new shared state or cycle.

## Acceptance Criteria
- [ ] AC1: After `revert_working_tree`, an untracked `docs/specs/x.md` still
  exists, while an untracked `src/...py` created by the step is gone.
- [ ] AC2: `heal_inline_comments` strips an inline trailing comment, preserving
  the code before it and the file's other lines, and returns that path.
- [ ] AC3: `heal_inline_comments` leaves a `noqa`-only or comment-free file
  unchanged and absent from its return list.
- [ ] AC4: A step emitting an inline comment in a contract file commits without
  failing the repo lint gate (heal runs before the gate).
- [ ] AC5: A step failing a lint-class gate on attempt 2 gets a third attempt; a
  step failing a non-lint gate on attempt 2 does not.
- [ ] AC6: `arch check`, ruff, and the no-inline-comments gate all pass; the
  full unit suite is green.

## Open Questions
- None.
