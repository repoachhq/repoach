---
id: SP-RELEASE-0-1-1-IS-CLEAN
title: 0.1.1 is built from the scrubbed tree with a narrowed sdist, its downloaded artefacts pass the personal-data lint, and the owner yanks 0.1.0 with a reason
version: 0.4
status: approved
author: agent
created: 2026-11-18
updated: 2026-11-18

owns:
  code: [tests/unit/test_sdist_is_narrow.py]
  resources: []

depends_on: [SP-SCRUB-PERSONAL-DATA-AT-HEAD, SP-NO-PERSONAL-DATA-GATE, SP-UNIVERSAL-INSTALL, SP-RELEASE-PR-CI-RUNS-FOR-THE-BOT-ACTOR, SP-LEDGER-FORMAT-IS-VERSIONED]
provides_to: []

constraints:
  release_version: 0.1.1
  yanked_version: 0.1.0
  sdist_only_include: [src/repoach, prompts, docs/specs/_TEMPLATE.md, README.md, LICENSE, licenses, pyproject.toml]
  sdist_top_level_names: 9
---

# 0.1.1 is clean, 0.1.0 is yanked

Amendment (0.2, 2026-11-18): the verifier's NO-GO on 0.1 re-measured.
hatchling always adds `.gitignore` to an sdist, so the top level has
nine names, not eight (F9, measured); the complete member list of the
0.1.0 sdist (F2); the yank oracle reads PyPI's JSON `yanked` field only,
since whether `pip index versions` hides a yanked release was not
measured.

Amendment (0.3, 2026-11-18): D2 counts child 3's allow entries as
eight, as child 3 0.2 declares them.

## Intent

Child 5 of SP-REPO-CARRIES-NO-PERSONAL-DATA. The published 0.1.0 sdist
is the whole tracked tree and carries the parent's F1, F2, F4, F5, F7
and F8 (parent F13). A published file cannot be replaced
(`docs/releasing.md:108-110`), so the remedy is a clean 0.1.1 whose
sdist holds what an installer needs and nothing else, proved by the
lint of child 3 over the downloaded artefacts, followed by the owner's
yank of 0.1.0 with a reason.

## Context

### Measured 2026-11-18

| # | Fact | Where |
|---|---|---|
| F1 | The wheel is narrow by configuration; the sdist has no configuration and is every VCS-tracked file | `pyproject.toml:88-93` (`[tool.hatch.build.targets.wheel]` `packages`, `force-include` of `prompts` and the spec template); no `[tool.hatch.build.targets.sdist]` |
| F2 | The 0.1.0 sdist: 5 026 285 B; top-level members `tests` 523, `docs` 415, `src` 272, `bench` 35, `.claude` 18, `scripts` 14, `prompts` 12, `.github` 11, `deploy` 6, `.githooks` 3, `assets` 3, `tmp` 1, `licenses` 1, and the files `.env.example`, `.gitignore`, `CLAUDE.md`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `LICENSE`, `PKG-INFO`, `README.md`, `SECURITY.md`, `pyproject.toml`, `repoach.toml`; personal hits in it, by the gate's rules and the owner's strings: home path 8 files (6 the maintainer's), system login 7, production address 1, `<host-a>` 5, `<host-b>` 2, layout 10, handle 38, service account 1, product domain 4 | `tar tzf repoach-0.1.0.tar.gz`; `grep -rl` over the extracted tree (`pip download --no-deps --no-binary :all: repoach==0.1.0`) |
| F3 | The wheel: 1 048 693 B, `repoach/` and `repoach/prompts/` only | parent F13 |
| F4 | The publish path builds once with `python -m build --sdist --wheel` and `twine check`, smokes the wheel, publishes from the `pypi` environment (required reviewer: the owner), then installs from the index | `.github/workflows/publish.yml:32-93,94-127,128-147,191-240`; `docs/releasing.md:44-49` |
| F5 | The wheel smoke installs the artefact in a clean `HOME` and runs it; `ci.yml` runs `wheel-build` and `wheel-smoke` on the `rows.wheel` prefixes | `scripts/wheel_smoke.sh:55-70`; `.github/affected-jobs.toml:57-60` |
| F6 | PyPI: `0.0.1` (1 394 B sdist, 1 569 B wheel, 2026-07-19, a name reservation) and `0.1.0` (2026-11-17); none yanked | `https://pypi.org/pypi/repoach/json` (re-read 2026-11-18) |
| F7 | The packaging metadata is pinned by a test that reads `pyproject.toml` | `tests/unit/test_packaging_metadata.py:14-30` |
| F8 | The release procedure: skew replay, release PR through the gate, optional dry run, tag `v<version>` on `main`, approve `pypi`, check the index | `docs/releasing.md:61-107` |
| F9 | With `[tool.hatch.build.targets.sdist] only-include` set to the seven entries of D1 on a `git archive` of `0be2a56`, `python -m build --sdist` produces a tarball whose top level is exactly nine names: `.gitignore LICENSE PKG-INFO README.md docs licenses prompts pyproject.toml src` — hatchling adds `.gitignore` and `PKG-INFO` itself; a wheel built from that sdist has the same 290 members as a wheel built from the tree (the verifier's count) | the verifier's build of 2026-11-18 under `tmp/`, its tarball re-listed by the spec owner (`tar tzf … \| cut -d/ -f2 \| sort -u` → the nine names) |

## Decisions (2026-11-18)

- D1. **The sdist is narrowed to what builds the wheel plus the licence texts.** `pyproject.toml` gains `[tool.hatch.build.targets.sdist]` with `only-include = ["src/repoach", "prompts", "docs/specs/_TEMPLATE.md", "README.md", "LICENSE", "licenses", "pyproject.toml"]`; hatchling adds `PKG-INFO` and `.gitignore` itself (F9). `tests`, `docs` (but the template), `bench`, `deploy`, `scripts`, `.github`, `.claude`, `.githooks`, `assets`, `tmp`, `repoach.toml`, `SECURITY.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` and `.env.example` leave the sdist: none is imported by the package (`[tool.hatch.build.targets.wheel]` already ships without them, F1, F3) and each is the repository's, not the distribution's. `tests/unit/test_sdist_is_narrow.py` builds the sdist with `python -m build --sdist` into a temporary directory (skipped with the reason when `build` is not importable; CI's `wheel-build` installs it) and asserts the top-level member set equals the nine names of F9 and that `docs/` holds exactly `specs/_TEMPLATE.md`.
- D2. **The artefact oracle is the lint of child 3.** `ci.yml`'s `wheel-build` job, after `python -m build`, extracts the sdist and the wheel into temporary directories and runs `scripts/lint_no_personal_data.py --tree <dir> --allow /dev/null` on each (no allow entries: of the eight entries of child 3 D4, none names a file the narrowed sdist or the wheel carries — `LICENSE` and `pyproject.toml` hold the real name, which is in no class and not in the deny-list — so the artefacts are judged with no exception at all, and the deny-list secret applies). `publish.yml`'s `pypi-smoke` job runs the same lint over `pip download --no-deps --no-binary :all: repoach==<version>` and `pip download --no-deps --only-binary :all: repoach==<version>`, extracted, before the pipx smoke, with the deny-list from the secret. The lint comes from the checked-out `scripts/` of the tag, which is why `pypi-smoke` checks out (`publish.yml:200-203`).
- D3. **The release is 0.1.1, by the documented procedure.** `docs/releasing.md:61-107` as written: skew replay, release PR `develop` → `main` through the release gate (owner merges, `release-verify.yml` checks `main`), tag `v0.1.1`, approve `pypi`. The release PR description carries, under *Measured*, the parent's AC1 output (arch baseline) and child 1's C1 lines. `hatch-vcs` with `release-branch-semver` (`pyproject.toml:84-86`) derives `0.1.1` from the tag; nothing in the tree names the version.
- D4. **The yank is the owner's, with a reason.** After `pypi-smoke` is green on 0.1.1 and AC2 is written, the owner yanks 0.1.0 on PyPI (project → Manage → Releases → 0.1.0 → Options → Yank) with the reason text: `Ships maintainer-personal paths, hosts and fixtures in docs/, tests/, bench/ and deploy/; superseded by 0.1.1 whose sdist holds the package only.` A yank keeps the files downloadable by exact pin (`pip install repoach==0.1.0` with a warning) and removes them from resolution without a pin, which is what the owner wants: no breakage for a pinned installer, no new installs of the leaking sdist. 0.0.1 stays: it is a 1 394-byte reservation with no content (F6).
- D5. **Owner steps are named as steps.** O1 approve the `pypi` deployment; O2 yank 0.1.0; O3 (optional) a note in the GitHub release of `v0.1.1` reading "0.1.0 yanked: see the PyPI reason". No agent holds the PyPI or the environment credentials (`docs/releasing.md:32-34`).
- D6. **`docs/releasing.md` gains two lines.** Under "Each release", step 6 adds "and `scripts/lint_no_personal_data.py --tree` over the downloaded sdist prints `personal-data: ok` (run by `pypi-smoke`)"; a new paragraph after `:110` documents the yank procedure of D4 as the remedy for a published file that must stop resolving.

## End state

`pip download --no-deps --no-binary :all: repoach==0.1.1` yields a
tarball whose `tar tzf` top level is the nine names `.gitignore`,
`LICENSE`, `PKG-INFO`, `README.md`, `docs` (`specs/_TEMPLATE.md` only),
`licenses`, `prompts`, `pyproject.toml`, `src`;
`scripts/lint_no_personal_data.py --tree` over its extraction and over
the wheel's prints `personal-data: ok` with the owner's deny-list
present; `pypi-smoke` on 3.11 and 3.13 is green;
`https://pypi.org/pypi/repoach/0.1.0/json` shows `yanked: true` with
the D4 reason on both files; the project JSON lists `0.0.1` and `0.1.1`
as the releases with an un-yanked file; `pipx install repoach`
resolves to 0.1.1.

## Easy solution rejected

Yanking 0.1.0 first: rejected, `pipx install repoach` would resolve to
the 1 394-byte 0.0.1 (F6) until 0.1.1 exists. Deleting 0.1.0: rejected,
a deleted version's filename can never be reused and a pinned
installer breaks; the yank keeps the pin working and stops new
resolution. Keeping the full-tree sdist because the tree is scrubbed:
rejected, the sdist of a Python package is the package's source, and a
future fixture with a personal string would ship again; the narrowed
sdist is defence in depth and the lint over the artefacts is the
proof. `MANIFEST.in`: rejected, hatchling does not read it; the
`[tool.hatch.build.targets.sdist]` table is the one place. Excluding
`.gitignore` with an `exclude` rule: rejected, hatchling adds it by
design and an eight-name assertion was the verifier's finding; the
test asserts what the tool produces (F9).

## Hard part

The two oracles live in two workflows on two events: `wheel-build` on
every affected pull request judges what *would* ship, `pypi-smoke` on
the tag judges what *did* ship, downloaded back from the index; both
must run the same lint with the same deny-list, from the checked-out
`scripts/`, so the lint must read a plain directory (no `git ls-files`)
— child 3's D2 fallback. The second hard part is sequencing with the
owner: the release, the environment approval and the yank are three
owner actions on two sites; the spec names them as steps with their
preconditions so that none is done early (a yank before 0.1.1 exists
is the Easy solution rejected).

## Goals

- G1: the sdist holds the package, its prompts, the template, the README, the licences, `pyproject.toml` and the two files hatchling adds, pinned by a test (D1).
- G2: the personal-data lint runs over the built and the downloaded artefacts in CI (D2).
- G3: 0.1.1 is released by the documented procedure from the scrubbed tree (D3).
- G4: 0.1.0 is yanked by the owner with the stated reason; 0.0.1 is left (D4).

## Non-Goals

- NG1: a new version scheme, changelog or release-notes change.
- NG2: deleting any file from PyPI.
- NG3: shipping `tests/` in the sdist (an installer never runs them; the repository is the test tree's home).
- NG4: removing the `.gitignore` hatchling adds (F9).

## Assumptions

- A1: children 1 and 3 have merged and `develop` is green.
- A2: `python -m build` is installable in CI (`publish.yml:61` installs it today).
- A3: PyPI's yank UI is as documented on 2026-11-18 (project owner, 2FA).

## Interface

Inputs:
- `pyproject.toml` `[tool.hatch.build.targets.sdist] only-include` (D1).
- `ci.yml` `wheel-build`: two new steps, `Extract sdist and wheel` and `Lint — no personal data in the artefacts (SP-NO-PERSONAL-DATA-GATE)`, with `env: REPOACH_PERSONAL_DENYLIST: ${{ secrets.REPOACH_PERSONAL_DENYLIST }}`.
- `publish.yml` `pypi-smoke`: one new step before the pipx smoke, same lint over `pip download`ed artefacts.

Outputs:
- `personal-data: ok (<n> files, 0 findings allowed by 0 entries, 0 stale)` twice per `wheel-build` run and twice per `pypi-smoke` matrix cell; exit 1 with the finding lines otherwise.

Errors:
- `pip download` fails (index not yet serving) → the existing wait loop (`publish.yml:211-226`) precedes the lint step, so the step sees a served release or the job is already red.

## Behavior

### Nominal

The pull request of this spec lands; `develop` → `main` through the
gate; `v0.1.1` tagged; `publish.yml` builds, smokes, waits for O1,
publishes, `pypi-smoke` downloads and lints; the owner performs O2 and
writes AC2–AC3.

### Edge cases

- The tag's `pypi-smoke` lint is red → the release is published already (the lint runs after the upload, since it judges the downloaded files); the owner yanks 0.1.1 too, with a reason naming the finding's class, and the fix is 0.1.2. `wheel-build`'s lint on the release PR is what prevents this; `pypi-smoke` is the proof, not the gate.
- A fork releases its own package → its secret is absent; the generic rules judge; its `only-include` is the same.

### Failure scenarios

- `python -m build --sdist` includes a file outside `only-include` plus the two hatchling adds (a hatchling behaviour change) → `test_sdist_is_narrow.py` red before any release.

### Operator steps

- O1 (owner): approve the `pypi` deployment of the `v0.1.1` run when it waits.
- O2 (owner, after AC2 is written): yank 0.1.0 on PyPI with the D4 reason; then `curl -s https://pypi.org/pypi/repoach/0.1.0/json | python -c 'import json,sys; print(sorted({(f["yanked"], f["yanked_reason"]) for f in json.load(sys.stdin)["urls"]}))'` → one tuple, `True` and the reason.
- O3 (owner, optional): edit the `v0.1.1` GitHub release notes to point at the yank.

## Tests

| Test | What it pins | Mutation that must turn it red |
|---|---|---|
| `tests/unit/test_sdist_is_narrow.py::test_sdist_top_level_is_the_package_its_licences_and_what_hatchling_adds` | D1: `python -m build --sdist` into a temp dir; `tar tzf` top-level set equals the nine names of F9; `docs/` holds only `specs/_TEMPLATE.md`; no member under `tests/`, `bench/`, `deploy/`, `.github/`, `.claude/`, `scripts/` | drop `only-include`, or add a path |
| `::test_pyproject_declares_only_include` | D1, without a build: `tomllib` parse, `[tool.hatch.build.targets.sdist].only-include` equals the seven entries | edit the list |
| `tests/unit/test_packaging_metadata.py` (existing) | F7: the `[project]` metadata is unchanged | strip a classifier |
| `tests/unit/test_affected_workflow_wiring.py` (existing) | D2: the two new `wheel-build` steps are gated on `job_wheel_build` like the job's other steps | leave a step ungated |
| `tests/unit/test_ci_portability.py` (existing) | the two workflows keep the configurable runner and the GitHub-hosted exception for `build-and-publish` | change a `runs-on` |

## Success conditions

- C1 (oracle: pytest, CI): `pytest -q tests/unit/test_sdist_is_narrow.py tests/unit/test_packaging_metadata.py tests/unit/test_affected_workflow_wiring.py tests/unit/test_ci_portability.py` → green.
- C2 (oracle: the `wheel-build` log of the release PR, URL written beside AC1): two `personal-data: ok (…, 0 findings allowed by 0 entries, 0 stale)` lines.
- C3 (oracle: the `pypi-smoke` logs of the `v0.1.1` run, URL written beside AC2): four `personal-data: ok` lines (sdist and wheel, 3.11 and 3.13), then the existing pipx smoke green.
- C4 (oracle: PyPI JSON, any machine, after O2): `curl -s https://pypi.org/pypi/repoach/0.1.0/json | python -c 'import json,sys; print({f["yanked"] for f in json.load(sys.stdin)["urls"]})'` → `{True}`; `curl -s https://pypi.org/pypi/repoach/json | python -c 'import json,sys; r=json.load(sys.stdin)["releases"]; print(sorted(v for v,fs in r.items() if any(not f["yanked"] for f in fs)))'` → `['0.0.1', '0.1.1']`.
- C5 (oracle: the downloaded sdist, any machine): `pip download --no-deps --no-binary :all: repoach==0.1.1 -d <tmp> && tar tzf <tmp>/repoach-0.1.1.tar.gz | cut -d/ -f2 | sort -u` → `.gitignore LICENSE PKG-INFO README.md docs licenses prompts pyproject.toml src` (nine names).

## Architecture Impact

- Adds dependency: SP-RELEASE-0-1-1-IS-CLEAN -> SP-SCRUB-PERSONAL-DATA-AT-HEAD (the tree it releases is the scrubbed one, A1)
- Adds dependency: -> SP-NO-PERSONAL-DATA-GATE (`scripts/lint_no_personal_data.py` is the artefact oracle, D2)
- Adds dependency: -> SP-UNIVERSAL-INSTALL (the wheel-install contract the sdist must keep satisfying; `pyproject.toml` build tables)
- Adds dependency: -> SP-RELEASE-PR-CI-RUNS-FOR-THE-BOT-ACTOR, -> SP-LEDGER-FORMAT-IS-VERSIONED (the release procedure of `docs/releasing.md` this spec follows and edits, D3, D6)
- New / changed coupling, cycles, or shared state: none; this spec owns one test. The 27 cycles and the 5 conflicts of the parent's F20 are unchanged.

## Diagram

```mermaid
flowchart LR
    T[scrubbed develop] --> B[wheel-build: build, extract, lint artefacts]
    B --> M[release PR → main, tag v0.1.1]
    M --> P[publish: pypi env approval O1]
    P --> S[pypi-smoke: download, lint, pipx]
    S --> Y[owner yanks 0.1.0 with reason O2]
```

## Acceptance Criteria

- [ ] AC1: C1 green; C2's URL and lines written here.
- [ ] AC2: C3's URL written here; C5's output written here.
- [ ] AC3: C4's two outputs written here by the owner after O2, with the yank reason as PyPI shows it.
- [ ] AC4: `repoach arch check --base origin/develop` → `edge-honesty: ok`; `repoach arch graph --check 2>&1 | grep -c '^dependency cycle'` → `27`; the `ownership conflict` lines are the parent's five.

## Open Questions

- None.
