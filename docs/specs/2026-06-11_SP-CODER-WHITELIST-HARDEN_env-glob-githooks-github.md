# SP-CODER-WHITELIST-HARDEN — close the .env.* glob and .githooks/.github gaps

## Metadata

- **Status**: OPEN
- **Priority**: P2
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-11

## Why

The audit found the bot write whitelist
(`src/ferova/review/coder_loop.py` — `FORBIDDEN_PATHS`,
`FORBIDDEN_PREFIXES`, `is_path_allowed`, also enforced for Developer
plan steps via `dev_runner`) narrower than the documented policy:

- `FORBIDDEN_PATHS` blocks only four exact names (`.env`,
  `.env.example`, `.env.local`, `.env.production`); the Coder/Developer
  prompts promise the whole `.env.*` family is rejected. `.env.staging`
  or `.envrc` would be written and committed.
- `FORBIDDEN_PREFIXES` blocks `.github/workflows/` but not the rest of
  `.github/` — bots can write `.github/CODEOWNERS` (review routing) or
  `.github/dependabot.yml`.
- `.githooks/` is writable, and the repo instructs
  `git config core.hooksPath .githooks`: a merged malicious hook is
  local code execution on the operator's next commit/push.

## What

In `src/ferova/review/coder_loop.py`:

1. Extend `is_path_allowed` with a basename rule: after normalisation,
   reject when the **final path component** is `.env`, starts with
   `.env.`, or is `.envrc` — anywhere in the tree, not just repo root.
   Keep the existing exact `FORBIDDEN_PATHS` entries (they remain true
   and self-documenting).
2. Extend `FORBIDDEN_PREFIXES` with `".github/"` and `".githooks/"`
   (the existing `".github/workflows/"` entry becomes redundant but is
   kept harmless, or dropped — Developer's choice; the docstring must
   state why the whole directories are protected: CODEOWNERS, actions,
   dependabot, hooksPath execution).
3. Update the `is_path_allowed` docstring to enumerate the new rules.

No call-site changes: `apply_fixes` and `dev_runner.execute_plan_step`
already route every write through `is_path_allowed`.

## Files in scope

- `src/ferova/review/coder_loop.py`
- `tests/unit/test_review_coder_loop.py`

## Out of scope

- Inverting to an allow-list of writable roots (`src/`, `tests/`,
  `docs/`, `scripts/`) — bigger policy change, candidate follow-up.
- Blocking `pyproject.toml` (bots legitimately add deps with code).
- Prompt text updates (`prompts/review/*` is itself whitelist-forbidden
  and the prompts already claim the stricter rule — code catches up to
  prompt).

## Smoke scenario

### Setup

None beyond imports — `is_path_allowed` is pure.

### Execute

Evaluate the predicate over: `.env.staging`, `tests/fixtures/.env.test`,
`.envrc`, `.githooks/pre-commit`, `.github/CODEOWNERS`,
`.github/dependabot.yml`, and the allowed controls
`src/ferova/review/coder_loop.py`, `docs/specs/example.md`,
`tests/unit/test_x.py`.

### Expected

All six hostile paths return False; the three controls return True;
every previously-tested rejection (absolute, traversal, workflows,
prompts) still returns False.

## Definition of Done

- Basename `.env*` rule covers root and nested paths —
  `test_is_path_allowed_rejects_env_family`.
- `.githooks/` and `.github/` (incl. non-workflows files) rejected —
  `test_is_path_allowed_rejects_github_and_githooks`.
- Existing allowed/rejected cases unchanged — current whitelist tests
  still green untouched.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `fix(review): whitelist rejects .env* family, .githooks/, all of .github/`
2. `test(review): hardened path-whitelist cases`

## Risks

- **Legitimate future work on `.github/`** (new workflows, dependabot)
  must be hand-shipped — already the de-facto policy for workflows;
  this makes it uniform.
- **Nested `.env`-named test fixtures** become unwritable by bots; use
  differently-named fixtures (`env_fixture.txt`) in tests.
