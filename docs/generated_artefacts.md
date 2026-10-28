# Generated artefacts: proved by their check, not read

A file that a committed script derives from other committed files cannot be
reviewed by reading it: its content is a function of its sources. A
regenerated embeddings file, for instance, changes one 1.5 MB line, and its
diff is twenty-two times what a reviewer is shown. Before
SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ such a file was dropped from every
reviewer's prompt and held the pull request with an oversized `spec_gap`
finding no push could clear.

The repository now declares the file with the command that proves the
derivation. The reviewers see one summary line instead of the file's diff,
and a secret-free job runs the command.

## Declaring an artefact

In `repoach.toml`, one `[[review.generated]]` table per artefact (at most 32):

```toml
[[review.generated]]
path = "api/data/knowledge_embeddings.json"
check = ["scripts/index_knowledge_corpus.py", "--repo-root", ".", "--check"]
sources = ["api/data/knowledge_corpus.json", "docs/user_guide/**"]
producer_inputs = ["api/data/_header.json"]
check_timeout_s = 120
```

| Key | Rule |
|---|---|
| `path` | A repository glob (`**` crosses directories). Never `..`, never a leading `/`. It may never match what the reviewers must read: a file of a language in `[project].languages`, a script (`.sh`, `.py`, `.js`, ...) or an extensionless file, a configuration format (`.toml`, `.yml`, `.ini`, `.xml`, `.lock`, ...) or a dotfile, CI and IDE definitions (`.github/`, `.gitlab/`, `ci/`, `.vscode/`, `Jenkinsfile`, ...), `repoach.toml`, dependency manifests and lockfiles at any depth, or any entry's check script, sources and producer inputs. A file the glob matches that does not exist at the base yet is reviewed in full: only a change to an existing artefact is summarised. A wildcard `path` is the maintainer's statement that the check proves every file it matches; repoach cannot see which files a check reads, so declare a literal path unless the check covers them all. |
| `check` | An argv list, never a string and never a shell. `check[0]` is a script tracked at the base: `.py` runs under `python3`, `.sh` under `bash`, anything else must be executable. Every later element matches `^[A-Za-z0-9._/=:@,+-]+$`; `{head}` and `{base}` are the only placeholders. |
| `sources` | Non-empty globs of the files the artefact derives from. |
| `producer_inputs` | Optional globs of files pinning the producer (a header carrying the model digest, a lockfile). A change to one counts as a source change and is reported apart. |
| `check_timeout_s` | Optional, seconds the check may run (default 120). |

A malformed entry is a `ConfigError` naming `review.generated[<i>].<key>` and
its line. The declarations are read from the **base branch**, never from the
pull request's head: an entry a pull request adds applies from the next pull
request on.

## What the review does

When the diff touches a declared artefact:

1. Every reviewer lens sees one paragraph in place of the artefact's diff:
   `generated artefact <path>: <size before> -> <size after> bytes, sha256 <before7> -> <after7>; sources changed in this diff: <list or none>; proved by: <check>`.
   No oversized finding is recorded for it.
2. The check never runs in the review job, which holds the forge token and
   the model credential. `repoach review generated` runs it in a separate
   job whose environment holds neither.
   - Each run is shell-free, with every secret-bearing variable removed.
   - Each run gets its own throwaway copy of the head, with its own git
     directory; the checkout, whose `.git` a later job would trust, is
     never used.
   - Each run has its own session, killed whole when the check exits; a
     descendant that detached itself is adopted and killed too.
   - A run that changes the artefact is stale, never verified.
   - A check script reached through a symlink is refused.
   - When the check script itself changed, the check runs twice, with the
     base script and with the head script, and both must pass. Only the
     script is swapped: both runs use the head tree, so a helper module the
     script imports is code in the diff, read by the reviewers. The head
     script is pull-request code run by the job's user, so the job image
     holds repoach installed by root and the job runs as another user
     (the snippet refuses otherwise); the evidence is renamed into place,
     so a link the check leaves at `--out` is never followed.
   - When the command holds a secret-bearing variable (`CI_JOB_TOKEN`
     included), it names it on stderr and re-runs itself without it, so
     the check cannot read it from repoach's own process.

   Outside repoach, the check could still read what the runner holds: the
   job token in the checkout's `.git/config`, where the runner writes it
   into the remote URL, and the job shell's environment. The job snippet
   below closes both, failing the job when it cannot:
   - it removes the credential from the remote URL and fails if any is
     left in the git configuration, an `include` or the environment;
   - it fails if any other process it can see still holds `CI_JOB_TOKEN`
     (on such a runner, start the job as another user than the CI shell,
     or mount `/proc` with `hidepid=2`);
   - it replaces the job shell itself (`exec env -i ...`) with
     `repoach review generated`, so the only process left that held the
     token no longer does.

   `--base` is required: the declarations are read at the base ref the
   job names, never from the checkout's `repoach.toml`, which is the pull
   request's.
3. The review recomputes everything it can derive (the argv from the base
   declaration, the changed sources from the diff, the state from the exit
   code), so the evidence is trusted for the check's exit code and output
   only. Those come from pull-request code: run the verification job on a
   runner that does not hand its builds directory to `review:gate` (a
   Docker executor, or a separate runner), since a same-user process can
   reach any path the job can.
4. `repoach review pr --generated-evidence review-generated.json` reads that
   job's evidence (refused when it names another head or another artefact
   blob), records one `generated_checks` row per run, and records one
   blocking `generated_artefact` finding per path whose derivation is not
   shown:

| Cause | `verification_result` |
|---|---|
| The check failed | `check exited <n>: <last line>` (`base check …` / `head check …` when the script changed) |
| The artefact changed, no source did | `no source changed` |
| The check script is absent at the head | `check script <path> absent at <sha7>` |
| The check outlived its timeout or the PR's 300 s budget | `check not run within <n> s: <path>` |
| No evidence for this head | `no check evidence for <path> at <sha7>` |

At a later head the finding settles from that head's row, never through the
refuter, and so does an older oversized finding on a path the base now
declares (`declared generated at <sha7>, verified by <check>`).

The report prints, per verified run,
`generated: <path> verified by <check> (<ms> ms)`, then
`generated checks: <checks_ms> ms + <overhead_ms> ms overhead`. The JSON of
`review pr` carries `generated: [{path, check, state, ms, sources_changed, producer_inputs_changed, script_ref}]`.

The check proves what the project's command proves, nothing more: a check that
re-derives a key and a shape says nothing about vector values it cannot
recompute.

## Wiring it in GitLab CI (Yoob)

The verification job carries no secret and runs an image of the project's
own toolchain (repoach does not install the project's dependencies) into which
repoach is installed, pinned, by root, with the job's commands running as
another user: the check is pull-request code and must not be able to rewrite
repoach. The job checks both before anything runs. The review job needs it and
reads its artefact. Secrets reach only the review job when they are scoped to
an environment that only `review:gate` declares; the verification job refuses
to run if any reached it.

```yaml
review:generated:
  needs: []
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  stage: review
  image: $YOOB_REVIEW_GENERATED_IMAGE
  timeout: 15 minutes
  variables:
    GIT_DEPTH: "0"
  script:
    - test -z "${REPOACH_GITLAB_TOKEN:-}${REPOACH_ANTHROPIC_AUTH_TOKEN:-}${REPOACH_ANTHROPIC_API_KEY:-}"
      || { echo "review:generated must hold no secret"; exit 2; }
    - test "$(id -u)" != 0 || { echo "review:generated: run the job as a non-root user"; exit 2; }
    - repoach_dir="$(python3 -c 'import os, repoach; print(os.path.dirname(repoach.__file__))')"
    - test ! -w "$repoach_dir" || { echo "review:generated: repoach is writable by the job user"; exit 2; }
    - git fetch --quiet origin "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
    - git remote set-url origin "$CI_PROJECT_URL.git"
    - test -z "${GIT_CONFIG_COUNT:-}${GIT_CONFIG_PARAMETERS:-}"
      || { echo "review:generated: git configuration injected through the environment"; exit 2; }
    - git config --get-regexp 'url\..*|remote\..*\.url|http\..*extraheader|credential\..*|include\..*|includeif\..*' | grep -qiE '@|authorization|helper|path'
      && { echo "review:generated: a credential is left in .git/config"; exit 2; } || true
    - |
      for environ in /proc/[0-9]*/environ; do
        [ "$environ" = "/proc/$$/environ" ] && continue
        if tr '\0' '\n' < "$environ" 2>/dev/null | grep -q '^CI_JOB_TOKEN='; then
          echo "review:generated: another process holds CI_JOB_TOKEN; isolate the job"; exit 2
        fi
      done
    - exec env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8
        repoach review generated
        --pr "$CI_MERGE_REQUEST_IID"
        --head "$CI_COMMIT_SHA"
        --base "origin/$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
        --out review-generated.json
  artifacts:
    when: always
    paths:
      - review-generated.json
    expire_in: 1 week

review:gate:
  needs: [review:generated]
  environment: review-gate
  # unchanged otherwise; ci/review-gate.sh adds
  #   --generated-evidence review-generated.json
  # to its `repoach review pr` call.
```

`needs: [review:generated]` adds the check's own time plus the job's start-up
to the gate; both are printed on the `generated checks:` line, so the delay is
read from the log, not guessed. A Yoob shell suite on `.gitlab-ci.yml` should
assert that `review:generated` names none of `REPOACH_GITLAB_TOKEN`,
`REPOACH_ANTHROPIC_AUTH_TOKEN` and `REPOACH_ANTHROPIC_API_KEY`, that it
resets the remote URL, runs the `.git/config` and `CI_JOB_TOKEN` process
checks, and starts `repoach review generated` with `exec env -i`, and that
`review:gate` needs it.
