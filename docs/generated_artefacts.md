# Generated artefacts: proved by their check, not read

A file that a committed script derives from other committed files cannot be
reviewed by reading it: its content is a function of its sources. A
regenerated embeddings file, for instance, changes one 1.5 MB line, and its
diff is twenty times what a reviewer is shown. Before
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
| `path` | A repository glob (`**` crosses directories). Never `..`, never a leading `/`, and never a glob that could match a file of a language in `[project].languages`: code is reviewed, not declared away. |
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
   the model credential. `repoach review generated` runs it in a separate job
   whose environment holds neither, with `shell=False` and every
   secret-bearing variable removed, in a throwaway clone of the head with
   its own git directory (never the checkout, whose `.git` a later job
   would trust), in its own session, killed whole when the check exits,
   with any descendant that detached itself adopted and killed too;
   when the check script itself changed, it runs twice, with the base
   script and with the head script, and both must pass. Only `check[0]`
   is swapped: a module the base script imports is read from the head
   tree, so a helper the pull request changes is code in the diff, read by
   the reviewers like any other (D3 runs both against the head tree). A check script
   reached through a symlink is refused. When the command itself holds a
   secret-bearing variable (`CI_JOB_TOKEN` included), it names it on
   stderr and re-runs itself from the scrubbed environment, so the check
   cannot read it from repoach's own process. It can still read what the
   runner holds outside repoach: the CI shell's environment and, where the
   runner writes it into the remote URL, the job token in the checkout's
   `.git/config`. The job snippet below therefore removes the credential
   from the remote URL and fails if any is left in `.git/config` before
   repoach starts; the job token itself stays in the CI shell's
   environment, so treat it as visible to the check and restrict the
   project's CI/CD job token permissions to reading this repository.
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

The verification job carries no secret and runs the project's own toolchain
image (repoach does not install the project's dependencies); the review job
needs it and reads its artefact. Secrets reach only the review job when they
are scoped to an environment that only `review:gate` declares; the
verification job refuses to run if any reached it.

```yaml
review:generated:
  needs: []
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  stage: review
  image: $YOOB_PYTHON_CI_IMAGE
  timeout: 15 minutes
  variables:
    GIT_DEPTH: "0"
    REPOACH_REF: e597bc678a1ff3ac0c2806fbe3617ba0c84b7fa5
  script:
    - test -z "${REPOACH_GITLAB_TOKEN:-}${REPOACH_ANTHROPIC_AUTH_TOKEN:-}${REPOACH_ANTHROPIC_API_KEY:-}"
      || { echo "review:generated must hold no secret"; exit 2; }
    - pip install --quiet "repoach @ git+https://github.com/repoachhq/repoach@${REPOACH_REF}"
    - git fetch --quiet origin "$CI_MERGE_REQUEST_TARGET_BRANCH_NAME"
    - git remote set-url origin "$CI_PROJECT_URL.git"
    - git config --get-regexp 'url\..*|remote\..*\.url|http\..*extraheader' | grep -q '@\|AUTHORIZATION'
      && { echo "review:generated: a credential is left in .git/config"; exit 2; } || true
    - repoach review generated
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
resets the remote URL and runs the `.git/config` credential check before
`repoach review generated`, and that `review:gate` needs it.
