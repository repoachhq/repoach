# W1 — repoach reviews a GitLab merge request, dry-run: what assumed GitHub

Measured 2026-10-18 on repoach `feat/sp-gh-glab-shim-dry-run` (from `develop`
`20dbb45`) against Yoob MR !283 (`josephwfaye/yoob_2`, merged, 36 files,
195 958 diff characters). Spec: `docs/specs/2026-10-18_SP-GH-GLAB-SHIM_disposable-gh-to-glab-read-shim.md`.
Worksite: `docs/plans/2026-10-18_yoob-production-plan.md` § 4, W1. Nothing
under `src/repoach` changed; the only adapter is `scripts/gh_glab_shim.py`,
installed as `gh` first on `PATH`.

## Run

| Item | Value |
|---|---|
| Command | `repoach review pr 283 --dry-run`, cwd `yoob_2`, `REPOACH_DB_PATH=<scratch>/yoob-review.db`, `REPOACH_LLM_PROXY_BASE_URL=http://127.0.0.1:8085` |
| Gateway | second instance on `:8085`, `MODEL_OPUS/SONNET/HAIKU=claude_code/<tier>` from `os.environ` (the `:8084` service untouched) |
| Exit, wall time | 0, 83 s |
| Reviews | 4 (architect, sentinel, tester, scribe); `posted_comments=0`, `posted_reviews=0`; final verdict `APPROVE` |
| Ledger | 4 `pr_reviews` rows, 12 `pr_findings` (9 proposed, 2 refuted, 1 verified) |
| Tokens (gateway `AGENT_DISPATCH`) | 225 582 across 7 calls: 5 reviewer calls ≈ 44 k each (scribe ran twice), 2 refuter calls 1.3–1.5 k |
| Forge calls the run made (shim log) | `pr diff 283` ×1, `pr view 283 --json number,title,body,headRefName,baseRefName,author,state,url` ×1, `api --paginate --slurp repos/:owner/:repo/pulls/283/comments` ×5, no write |
| MR notes before/after | 2 / 2 |

Plan: `repoach plan SP-YOOB-TEMPLATE-HONESTY --explore-via claude_cli` in a
detached Yoob worktree at `develop` `7d93499a` (the merge of !283), spec
written from the template-honesty brief. First run: exit 1 after 5/5
attempts (B1), 250 s. After the spec named a `tests/integration/` file:
exit 0, 232 s, 7 steps, 14 distinct files (3 existing, 11 new), 2
integration selectors.

Calls a dry run would make on other MRs, never reached here: `pr view --json
baseRefName,headRefOid` (diff fallback, `gh_client.py:295`), `git fetch
origin refs/pull/<N>/head` (`gh_client.py:324`), `git fetch` of the base for
spec coverage (`orchestrator.py:698`), only when a spec is resolved.

## GitHub assumptions hit

| # | Assumption | Where | What GitLab gives instead | What a `Forge` protocol must expose |
|---|---|---|---|---|
| A1 | The forge is the `gh` binary on `PATH`; no other forge can be selected | `review/gh_client.py:155` | `glab`, or REST with a token | a `Forge` selected by config (`REPOACH_FORGE`), injected into the orchestrator |
| A2 | A PR diff is `gh pr diff N`, and the diff carries `diff --git a/… b/…` headers that the scoper and the findings bridge split on | `gh_client.py:263`; `diff_scoper.py:48`; `findings_bridge.py:150` | `glab mr diff 283` prints 0 `diff --git` lines (glab 1.36); `GET merge_requests/:iid/raw_diffs` prints 36 | `diff(change_id) -> str` in git's format, whatever the transport |
| A3 | PR metadata uses GitHub's field names `number`, `headRefName`, `baseRefName`, `headRefOid`, `body`, `url` | `gh_client.py:295,364,688` | `iid`, `source_branch`, `target_branch`, `sha`, `description`, `web_url` | a typed `ChangeRequest` model (id, title, body, head ref, base ref, head sha, author, state, url) |
| A4 | Author identity is `author.login` / `user.login` | `gh_client.py:65-81` | `author.username` | `author: str` on the model; trust checks compare it |
| A5 | State vocabulary is `OPEN/MERGED/CLOSED` | `review/auto_merge.py:832` | `opened/merged/closed/locked` | an enum on the model, mapped per forge |
| A6 | Inline threads are `pulls/N/comments` with `in_reply_to_id`, `path`, `line` | `gh_client.py:636`; `thread_context.py:156` | discussions whose `DiffNote` notes carry `position.new_path/new_line`; the root is the first note, there is no `in_reply_to_id` | `inline_threads(change_id) -> list[Thread]` (root + replies, path, line, author) |
| A7 | A review is an object with an id and its own comments | `gh_client.py:599` (`reviews/{id}/comments`), `:548-549` (`commit_id`, `event`) | no review object; an approval is separate from notes; a diff note is anchored by `position` (`base_sha`, `start_sha`, `head_sha`) | `submit_review(verdict, comments)` returning the posted thread ids, the anchoring left to the forge |
| A8 | Inline comments anchor on `side: RIGHT` + `line` | `gh_client.py:554-555` | `position{position_type: text, new_line, new_path, base_sha, start_sha, head_sha}` | the anchor as `(path, new_line, head_sha)`; each forge builds its own payload |
| A9 | Archive / sticky comment is an issue comment found by marker and author | `gh_client.py:719,773,826` | MR notes (`merge_requests/:iid/notes`), `system` notes must be filtered | `find_note(marker, author)`, `upsert_note(marker, body)` |
| A10 | The PR head is fetchable as `refs/pull/<N>/head` | `gh_client.py:324` | `refs/merge-requests/<iid>/head` | `fetch_head(change_id)` or the head ref name |
| A11 | The trusted bot identity is `github-actions[bot]` | `core/config.py:224` | the token user's `username` | `bot_identity` per forge |
| A12 | Spec is found from a branch named `<kind>/sp-<id>-…` | `review/spec.py:46` | Yoob branches are `<kind>/<theme>`; !283 (`feat/wire-spi-gpio-i2c-into-the-soc`) resolves no spec, so the run had no spec coverage | `--spec`, or a `Spec:` line in the change description, read through the forge |
| A13 | CI status is `statusCheckRollup` with fixed check names | `auto_merge.py:177-188` | `head_pipeline.status` | `ci_status(change_id, head_sha)` (not reached by a dry run) |
| A14 | Merge is `gh pr merge --squash` | `auto_merge.py:450` | Yoob merges with merge commits | `merge(change_id, method)` (not reached by a dry run) |
| A15 | CLI help and user text say "GitHub PR number" | `cli/review_cmds.py:67,128` | MR `iid` | wording only |

Two findings are about the dry run itself, not GitHub:

- **A dry run is not write-free.** `post_refuted_finding_sentinels` runs
  outside the `if self._post:` guard (`orchestrator.py:661` versus `:768`)
  and calls `reply_to_review_comment` (a `POST`) for each refuted finding
  that matches an existing inline thread. Here 2 findings were refuted and
  the 5th `pulls/283/comments` read is that step; it posted nothing only
  because the MR has no inline thread, and the shim would have refused the
  write with exit 70. On a GitHub PR with repoach threads, `--dry-run` posts.
- **A dry run has no head SHA.** The head guard starts only when posting
  (`orchestrator.py:410-414`), so the ledger rows carry `head_sha=NULL` and
  the verdict is derived from facts at an empty head. The run reported
  `APPROVE` while the scribe left 2 blocking findings in `proposed`. The
  reviewers' file reader also reads the cwd tree (`orchestrator.py:326`),
  which is the Yoob checkout's current branch, not the MR head.

## Not GitHub, but in the way on another repository

| # | Fact | Where |
|---|---|---|
| B1 | The planner requires plan-level integration tests under `tests/integration/` for any plan touching `src/`; Yoob has no such tree. The first `repoach plan` run failed 5/5 attempts on that rule; the spec had to name the directory before a plan could validate | `review/plan.py:354-358,406-418` |
| B2 | `review pr` writes structlog lines and the JSON payload to the same stdout, so the payload has to be cut out of the stream | `cli/review_cmds.py:118` (`typer.echo` of the payload) next to the structlog console renderer on stdout |
| B3 | Every reviewer answered in one turn with 0 tool calls on the `claude_code` tail, so none of them explored the Yoob tree | gateway `AGENT_DISPATCH ... n_tool_calls=0` (7/7) |
| B4 | `planner_attempts` and the review tables follow `REPOACH_DB_PATH`, which isolates a project only if every invocation sets it; the default is anchored to the repoach checkout | `core/config.py:138-141,301-317` |
| B5 | The planner's `claude_cli` path reports `tokens_used: 0`, so its cost is invisible in the ledger | `repoach plan` payload, this run |
| B6 | The validated plan names files that do not exist where the real ones do: `src/yoob/codegen/amp_bare_metal.py` (real: `src/yoob/hw/soc/firmware_amp.py`), `src/yoob/config/arch_config.py` (real: `src/yoob/hw/soc/schema.py`), `api/routes/schedule.py` (real: `schedules.py`); frontend tests get pytest-style `file::name` selectors for Vitest. Selector validation checks shape and creation order, not that a step edits an existing module | `docs/plans/SP-YOOB-TEMPLATE-HONESTY.md` in the Yoob worktree; `review/plan.py:361-380` |

## What the `Forge` protocol needs, from this run

`diff`, `change_request` (typed model, A3–A5), `inline_threads`,
`submit_review` with forge-built anchors (A7–A8), `find_note`/`upsert_note`
(A9), `fetch_head` (A10), `bot_identity` (A11), `spec_hint` from the
description (A12), `ci_status` (A13), `merge(method)` (A14). The five test
fakes listed in W2 already cover the call surface; the field mapping above is
what the GitLab implementation must do, and `scripts/gh_glab_shim.py` is its
executable sketch, to delete when W2 lands.
