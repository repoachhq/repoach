---
id: SP-REPOACH-INIT-SCAFFOLD
title: repoach init — scaffold .env and git hooks for a fresh clone
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: [src/repoach/cli/init_cmds.py]
  resources: N/A

depends_on: []
provides_to: []

constraints: {}
---

# repoach init — scaffold .env and git hooks for a fresh clone

## Intent

A third party cloning the now-public repo has no in-code onboarding
path: `.env` must be hand-copied from `.env.example`, the guardrail
git hooks must be wired by hand, and the remaining manual steps
(provider key, install) are only discoverable by reading
`docs/getting_started.md` end to end. Add a `repoach init` Typer
command that performs the two mechanical, idempotent steps and prints
the steps that genuinely require a human (a secret can't be
scaffolded).

## Context

Finding #20 (usability audit), evidence re-verified against
`origin/develop@bc4e4e0` (2026-07-31):

- `src/repoach/cli/main.py` registers `version`, `monitor-chains`,
  `autopilot`, `chains-audit`, `regenerate-chains`, plus the
  `review`/`release`/`arch`/`memory` Typer groups (`git show
  origin/develop:src/repoach/cli/main.py | grep "@app.command\|
  add_typer"`) — no `init` command exists, and no scaffold-generation
  code exists under `src/repoach/cli/` (`ls` shows only
  `chain_status.py`, `main.py`, `release_cmds.py`, `review_cmds.py`).
- `docs/getting_started.md:66-71` (section 3, "Activate the
  guardrails") requires the operator to type
  `git config core.hooksPath .githooks` by hand.
- `docs/getting_started.md:73-79` (section 4, "Configure secrets")
  requires `cp .env.example .env` by hand, then setting
  `REPOACH_ANTHROPIC_AUTH_TOKEN` and one provider key — these last two
  are secrets and cannot be scaffolded; they stay manual.
- `docs/getting_started.md:97`: `REPOACH_DB_PATH=./data/repoach.db
  # SQLite; auto-created on first use` — the data directory needs no
  scaffolding; `.gitignore:38` confirms `data/` is untracked.
- `chains.env` (repo root) is already a tracked, version-controlled
  file present on every `git clone` — `git status` / `git ls-files`
  confirm it ships with the checkout, so there is no "copy chains.env"
  gap despite the audit's proposed direction assuming one (see
  Non-Goals).
- `src/repoach/cli/main.py:62-90` shows the established pattern for a
  new top-level command: a plain function decorated or registered via
  `app.command(name=...)(callable)`, plus a module-level factory
  (`_probe_client`) "extracted ... so tests can override it" —
  the same override-for-testability shape this spec reuses for
  `_repo_root`.

`main.py` is not owned by any existing spec (grepped every
`docs/specs/*.md` for `src/repoach/cli/main.py` — zero hits); this
spec registers one command there without claiming ownership of the
file, matching the precedent set by `SP-CHAIN-STATUS-DIGEST` (owns
only `chain_status.py`, registers a command in `main.py` without
claiming it).

## Goals

- G1: `repoach init` creates `.env` from `.env.example` when `.env`
  does not already exist at the repo root; idempotent — re-running it
  never overwrites an existing `.env`.
- G2: `repoach init` runs `git config core.hooksPath .githooks` at the
  repo root, activating the pre-commit/pre-push guardrails documented
  in `docs/getting_started.md` section 3.
- G3: `repoach init` prints the remaining manual steps in its stdout
  (set `REPOACH_ANTHROPIC_AUTH_TOKEN` + one provider key in `.env`,
  `pip install -e ".[dev]"`, `repoach version` to verify) so a fresh
  clone's onboarding is discoverable from `repoach --help` without
  reading the docs first.
- G4: the command is registered on the top-level Typer `app` as
  `init` and discoverable via `repoach --help`.

## Non-Goals

- NG1: no `chains.env` copy/scaffold step — it is already tracked and
  present after `git clone` (see Context); nothing to scaffold.
- NG2: no `data/` directory creation — `REPOACH_DB_PATH` is
  auto-created on first use (`docs/getting_started.md:97`);
  scaffolding it here would be a no-op wrapped in false busywork.
- NG3: no writing of secret values into `.env` — provider keys and
  `REPOACH_ANTHROPIC_AUTH_TOKEN` remain manual, printed as follow-up
  steps only.
- NG4: no change to `docs/getting_started.md` or `README.md` — this
  spec ships the command; documenting it in the guide is a separate,
  editorial follow-up left to the operator.
- NG5: no behavior change to any existing CLI command; this spec adds
  exactly one new command and one new module.
- NG6: no interactive prompts (no `typer.prompt` for the token) — this
  is a non-interactive, scriptable scaffold command; secrets stay
  manual per NG3.

## Interface

New module `src/repoach/cli/init_cmds.py`:

```python
def _repo_root() -> Path:
    """Return the repository root (`.env`/`chains.env` anchor).

    Extracted as a module-level function, mirroring
    `main.py::_probe_client`, so tests can monkeypatch it to a
    temporary directory instead of mutating the real checkout.
    """

def scaffold_env(repo_root: Path) -> bool:
    """Copy `.env.example` to `.env` under `repo_root` if `.env` is absent.

    Args:
        repo_root: Directory containing `.env.example` and (maybe) `.env`.

    Returns:
        True if `.env` was created by this call; False if it already
        existed and was left untouched.

    Raises:
        FileNotFoundError: `.env.example` is missing from `repo_root`.
    """

def configure_hooks(repo_root: Path) -> subprocess.CompletedProcess[str]:
    """Run `git config core.hooksPath .githooks` in `repo_root`.

    Raises:
        subprocess.CalledProcessError: `git config` failed (e.g.
            `repo_root` is not inside a git working tree) — surfaced,
            never swallowed.
    """

def init() -> None:
    """Typer callback for `repoach init` — scaffold + print next steps."""
```

`src/repoach/cli/main.py`:
- `from .init_cmds import init` and `app.command(name="init")(init)`,
  next to the existing `app.command(name=...)` registrations.

## Behavior

### Nominal

- Fresh clone, no `.env` present: `repoach init` creates `.env` from
  `.env.example`, configures `core.hooksPath`, prints the manual
  follow-up steps, exits 0.

### Edge cases

- `.env` already exists (re-run, or the operator already configured
  it): `scaffold_env` returns `False`, the existing file's bytes are
  untouched, `init` prints that `.env` already exists rather than a
  creation message; exit code still 0.
- `core.hooksPath` already set to `.githooks`: `git config` is a no-op
  overwrite of the same value — no error, no duplicate side effect.
- Run from a subdirectory of the repo: `_repo_root()` resolves via
  `Path(__file__)`, not CWD, so the result is identical regardless of
  the invoking directory.

### Failure scenarios

- `.env.example` missing from the repo root (corrupted checkout):
  `scaffold_env` raises `FileNotFoundError` naming the missing path;
  `init` does not catch it — the command fails loud with a
  traceback rather than silently skipping the scaffold step
  (SP-LINT-LOG-CATCH-ALL: no silent except).
- `repo_root` is not inside a git working tree (e.g. an extracted
  tarball with no `.git`): `configure_hooks`'s `subprocess.run(...,
  check=True)` raises `CalledProcessError`, surfaced with git's own
  stderr; not swallowed.

## Architecture Impact

- Adds one new leaf module (`src/repoach/cli/init_cmds.py`, owned by
  this spec) with no intra-repo imports beyond stdlib + `typer` — no
  new owned-to-owned coupling.
- One new import line in `main.py` (`from .init_cmds import init`);
  `main.py` stays frontier/unowned, consistent with every other
  command-registration spec in this repo (`SP-CHAIN-STATUS-DIGEST`
  precedent) — no `depends_on` edge required for either spec.
- No new cross-owner cycle; no shared mutable state.

## Diagram

N/A (single new leaf CLI module + one registration line).

## Acceptance Criteria

- [ ] AC1: unit — `scaffold_env(tmp_path)` with a fixture
  `.env.example` present and no `.env`: creates `.env` with the
  fixture's exact content and returns `True`. Called again against the
  same `tmp_path` after mutating `.env`'s content: returns `False` and
  leaves the mutated content untouched (no overwrite).
- [ ] AC2: unit — `configure_hooks(tmp_path)` against a `git init`-ed
  `tmp_path`: after the call, `git -C tmp_path config --get
  core.hooksPath` reports `.githooks`.
- [ ] AC3 (INTEGRATION): drive `repoach init` end-to-end via
  `typer.testing.CliRunner().invoke(app, ["init"])` against a
  `tmp_path` that has been `git init`-ed and seeded with a real
  `.env.example` fixture and no `.env`, with
  `repoach.cli.init_cmds._repo_root` monkeypatched to return
  `tmp_path` — assert exit code 0, `tmp_path / ".env"` now exists with
  the fixture's content, `git -C tmp_path config --get
  core.hooksPath` reports `.githooks`, and stdout contains the
  provider-key/`pip install`/`repoach version` follow-up lines.
- [ ] AC4: promised tests —
  `tests/unit/test_init_cli.py::test_init_command_is_registered`,
  `::test_scaffold_env_creates_file_from_example`,
  `::test_scaffold_env_does_not_overwrite_existing_env`,
  `::test_configure_hooks_sets_core_hooks_path`, and
  `tests/integration/test_init_cli.py::test_repoach_init_scaffolds_fresh_clone_end_to_end`.
  `test_init_command_is_registered` MUST FAIL on pre-change code (no
  `init` command exists on the current `app`).
- [ ] AC5: `ruff check` + `ruff format --check` +
  `pytest tests/unit tests/integration/test_init_cli.py` green; zero
  inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`; `repoach
  arch check` exits 0 (new file is a clean leaf, no undeclared owned
  coupling).

## Open Questions

(none)
