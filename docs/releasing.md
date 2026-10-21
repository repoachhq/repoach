# Releasing repoach to PyPI

`SP-PYPI-RELEASE`. A `v*` tag pushed on a commit of `main` publishes that
commit to PyPI. Nothing else publishes: no branch push, no PR, no manual
upload. This page is the owner's procedure and what each check proves.

## Where the version comes from

The git tag is the only source. `pyproject.toml` declares
`dynamic = ["version"]` and `hatch-vcs` reads the tag at build time;
`repoach.__version__` reads the installed metadata.

| Checkout | `repoach --version` |
|---|---|
| exactly on `v0.1.0` | `repoach 0.1.0` |
| N commits after the last tag (or with no tag yet) | `repoach 0.1.0.devN+g<sha>` (the next minor, `release-branch-semver`) |
| uncommitted changes | `…+g<sha>.d<YYYYMMDD>` |

An editable install records the version at install time: re-run
`pip install -e ".[dev]"` after a new tag to see it.

## One-time setup (owner)

These steps need the owner's accounts; no agent can do them.

1. **Claim or recover the PyPI project.** Sign in to the PyPI account that
   holds the `repoach` name reservation (`repoach 0.0.1`). If the account is
   lost, recover it through PyPI's account recovery; if the name was never
   yours, a PEP 541 request or a new project name are the only routes.
2. **Enable 2FA** on that account (PyPI requires it to manage publishers).
3. **Add the trusted publisher** on PyPI: project `repoach` → *Manage* →
   *Publishing* → *Add a new publisher* → GitHub, with
   - owner `repoachhq`
   - repository `repoach`
   - workflow `publish.yml`
   - environment `pypi`
4. **Create the GitHub environment** `pypi`: repository *Settings* →
   *Environments* → *New environment* `pypi` → *Required reviewers*: the
   owner; *Deployment branches and tags*: tags matching `v*`. The upload job
   then waits for that approval on every release.
5. **Choose the runner** (spec decision D4). Every job runs on
   `vars.REPOACH_RUNNER`, `self-hosted` when unset: the owner's machine must
   be up for a release. Setting the repository variable to `ubuntu-latest`
   moves `ci.yml` too, so a release-only change belongs in `publish.yml`.

## Each release

1. Merge the release PR `develop` → `main` through the release gate
   (`repoach release gate --pr <N>`, then the owner merges;
   `release-verify.yml` checks the shape of `main` after the merge).
2. Optional dry run: *Actions* → *Publish to PyPI* → *Run workflow* on
   `main`. It builds, prints `dry run: wheel <version>, no tag`, and runs the
   wheel smoke on 3.11 and 3.13; it never publishes nor creates a release.
3. Tag `main` and push the tag:

   ```bash
   git fetch origin main
   git checkout main && git pull --ff-only origin main
   git tag v0.1.0
   git push origin v0.1.0
   ```

   A pre-release uses a PEP 440 tag, e.g. `v0.1.0rc1`: PyPI marks it a
   pre-release, `pipx install repoach` skips it and
   `pipx install repoach==0.1.0rc1` installs it. Use the canonical spelling
   (`rc1`, not `-rc.1`): the version check compares strings.
4. Approve the `pypi` deployment when the run asks for it.
5. Check: `pip index versions repoach` lists the version, and the release
   page carries the generated notes.

A published version cannot be replaced. If anything after the upload is
wrong, fix it on `develop` and release the next version with a new tag;
never delete and re-push a tag that reached PyPI.

## What the workflow checks, in order

| Job | Check | A failure means |
|---|---|---|
| `build` | the tagged commit is an ancestor of `origin/main` | the tag is not on `main`; nothing is built |
| `build` | `python -m build` then `twine check` | the metadata or README would be rejected by PyPI |
| `build` | wheel version == tag without its `v` | `version mismatch: wheel X, tag Y`; nothing is published |
| `wheel-smoke (3.11)`, `(3.13)` | `scripts/wheel_smoke.sh` on the built wheel | the wheel does not install or run outside a checkout |
| `build-and-publish` | trusted publishing from the `pypi` environment | `invalid-publisher` when step 3 above is missing; the version already exists |
| `github-release` | `scripts/release_notes.py <previous v-tag> <tag>` attached to the release | — |
| `pypi-smoke (3.11)`, `(3.13)` | the index serves the version within 10 minutes, then the same smoke installs it from `https://pypi.org/simple` | the upload is not installable from PyPI |

`ci.yml` runs `wheel-build` and `wheel-smoke (3.11)` / `(3.13)` on every PR,
so a change that breaks the wheel is red before it reaches `develop`.
Locally: `scripts/ci_local.sh --wheel`.

## What the smoke proves, and what it does not

`scripts/wheel_smoke.sh` installs the wheel with `pipx` into a fresh `HOME`,
`PIPX_HOME` and `PIPX_BIN_DIR`, with `PYTHONPATH`, `VIRTUAL_ENV` and the
provider variables unset, then, from directories outside any checkout:

- `repoach --version` prints exactly `repoach <expected version>`: the
  version came from the tag through the metadata, not from a literal;
- `repoach --help` exits 0: every CLI module imports from the wheel;
- `repoach doctor` exits 0 in an empty `git init` repository with
  `REPOACH_PROVIDER=ollama` and dummy model names: settings, project
  config, ledger path and provider resolution work with no `.env`, no
  `repoach.toml` and no gateway (`SP-UNIVERSAL-INSTALL`);
- `repoach init --ci gitlab --print` exits 0 and pins
  `repoach==<expected version>`: the shipped templates are in the wheel and
  a wheel install pins itself from PyPI, not from a git checkout.

It does not prove that a review runs: no model is called, no forge is
contacted, and `doctor` needs `gh` on the runner's `PATH` (the forge it
checks is `github`). It does not check that every runtime data file is in
the wheel (`docs/tech_debt.md`).
