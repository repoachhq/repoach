# Contributing to Repoach

Thanks for your interest in Repoach! This repo works a little
differently from most open-source projects, so please read this page
before opening a PR — it will save you (and the bots) a round-trip.

## How this repo is built

Repoach is built *by* Repoach. The primary developer is the factory
itself: substantive changes start as a **governed spec** in
`docs/specs/`, a Planner turns the spec into a step-by-step action
plan, and an autonomous Developer implements it branch-by-branch,
commit-by-commit. Every PR — bot-authored or human-authored — then
goes through the same pipeline: a four-reviewer bot team (Architect /
Sentinel / Tester / Scribe), a findings ledger, and an
**evidence-first merge gate** that re-verifies every fact (CI green,
zero surviving blocking findings, spec coverage) at the exact head it
is about to merge.

The best ways to contribute, in increasing order of ceremony:

1. **Open an issue** — bug reports, design discussion, spec proposals.
   This is the right starting point for anything substantive.
2. **Improve docs** — docs-only PRs are welcome and cheap to review.
3. **Propose a spec** — if you want a feature built, a well-formed
   spec in `docs/specs/` (see the conventions below) is worth more
   than an unsolicited implementation: the factory can build from it.
4. **Code PRs** — welcome too; expect the bot review team to review
   your diff the same way it reviews its own.

## Branches and PRs

- `main` is release-only; **all PRs target `develop`**.
- Branch naming: `feat/…`, `fix/…`, `chore/…`.
- Commit messages follow conventional-commit style
  (`feat(scope): …`, `fix(scope): …`, `docs(specs): …`) in English.
- One concern per PR; small is beautiful — the review bench re-reads
  your whole diff.

> **Note on forks:** CI and the review team run on self-hosted
> runners, so **workflows do not run automatically on PRs from
> forks**. A maintainer will pick up your PR, mirror the branch into
> the repo, and run the pipeline on it. Nothing is wrong with your PR
> if checks show as skipped — it just needs the maintainer hop.

## Local setup

```bash
git clone https://github.com/repoachhq/repoach && cd repoach
pip install -e ".[dev]"            # Python 3.11+
git config core.hooksPath .githooks   # pre-commit + pre-push gates
cp .env.example .env               # fill in your provider keys
```

## Quality gates (run these before pushing)

```bash
scripts/ci_local.sh          # full parity with CI
scripts/ci_local.sh --fast   # lint-only (ruff + format + comment gate)
scripts/ci_local.sh --tests  # pytest-only
```

The gates that will actually bite you:

- **Zero inline comments, zero `# noqa`** anywhere in `src/`,
  `tests/`, `scripts/`. Substantive *why* belongs in docstrings
  (Google style); well-named identifiers carry the *what*. Lint
  exceptions live in `pyproject.toml` only. Enforced by pre-commit,
  CI, and a dedicated test.
- **No silent `except`** — every caught exception is logged or
  re-raised.
- **Strict typing** everywhere; **Pydantic models** for data crossing
  module boundaries.
- **English everywhere** — code, comments, docstrings, log messages,
  specs, tests, commit messages.
- **No secrets in code** — everything via `.env`; env vars use the
  `REPOACH_*` prefix. Never put a real key in a test fixture.
- `pytest tests/unit` must be green; integration tests live under
  `tests/integration/`.

## Specs

Substantive changes are anchored to a spec:
`docs/specs/<date>_<SP-ID>_<slug>.md`, with YAML frontmatter declaring
`owns` (the modules the spec owns) and `depends_on`. Repoach *derives*
the dependency graph from these declarations and *enforces* it in CI
— an import crossing an undeclared boundary fails the build. Browse
recent specs in `docs/specs/` for the format; `repoach plan <SP-ID>`
and `repoach develop <SP-ID>` are how the factory builds from one.

## What reviewers (bots and humans) will hold you to

- The diff matches a spec's acceptance criteria, or is a genuinely
  minor fix.
- Tests are real: truthful boundary fakes (a fake HTTP transport, a
  fake CLI executable) are fine; monkeypatching Repoach's own
  behavior to make a test pass is not.
- CI green at head, no surviving blocking findings — the merge gate
  re-checks this mechanically; there is no human override in the
  normal path.

## License

By contributing, you agree that your contributions are licensed under
the [MIT License](LICENSE).
