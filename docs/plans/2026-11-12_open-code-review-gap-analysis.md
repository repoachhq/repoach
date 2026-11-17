# open-code-review and repoach: what each does, what repoach takes from it

Read on 2026-11-12. open-code-review (OCR, Alibaba) at commit
`6386895e7cfac419bd7a9cf4795e8b824a6e3ced` (shallow clone, files read,
nothing run); repoach at `origin/develop` `8fabb19`. `R/` is the OCR
clone root, paths without a prefix are repoach's. A cell that rests on
something not read in a file says `not verified`.

## Licence

| Fact | Where |
|---|---|
| OCR is Apache License 2.0, "Copyright 2026 alibaba/open-code-review Contributors"; every Go file carries `SPDX-License-Identifier: Apache-2.0`, enforced by `R/scripts/verify-license.sh` | `R/LICENSE:1-3,189`, `R/internal/agent/agent.go:1-2` |
| Contributing to OCR requires the Alibaba Open Source Contributor License Agreement | `R/CONTRIBUTING.md:244,273-277` |
| Governance: lazy consensus, a Project Lead with the final say; the review engine, CLI and config formats need wider review | `R/GOVERNANCE.md` |
| The AACR-Bench dataset the README's numbers come from is `license: apache-2.0` on Hugging Face (`Alibaba-Aone/aacr-bench`, API read 2026-11-12) | `R/README.md:53-60`; HF dataset card |
| Embedded third-party material: Ant Design icons (MIT, `R/internal/viewer/static/icons/NOTICE`); `cl100k_base.tiktoken` data with no licence note found | not verified beyond the NOTICE |

What this allows. Ideas and designs are not covered by copyright; every
spec below re-derives its design on repoach's data model and copies no
OCR code, prompt or rule text. Apache-2.0 code or text can be carried
into an MIT project only with the Apache notice and licence kept for
that portion (repoach already does this for one dependency,
`licenses/free-claude-code-LICENSE`), and with a NOTICE line when OCR
ships one (it ships none at the root). The one place where copying
would tempt is the 64 per-language checklists under
`R/internal/config/rules/rule_docs/`; `SP-REVIEW-RULES-BY-PATH` writes
repoach's own and leaves the copy as an owner decision. Nothing of
repoach's needs to flow back: the CLA concerns contributions to OCR,
not use of it.

## The two designs in one paragraph each

**OCR** is one Go binary (`ocr`, `R/cmd/opencodereview/`) that reviews a
git range with one model: files are selected deterministically
(`R/internal/agent/selection.go`), grouped (an LLM grouping call when
there are four files or more, `R/internal/config/template/template.go:130`),
and each group is reviewed by one agent running a tool loop over
`code_search` (`git grep`), `file_read`, `file_find` and
`file_read_diff` (`R/internal/config/toolsconfig/tools.json`), with a
plan phase for large groups and up to three rounds. A comment names the
code it is about (`existing_code`) and is placed on a line by matching
that snippet against the hunk, then the file, then another file, then
an LLM relocation call (`R/internal/diff/resolver.go`,
`relocation.go`). One fact-checking LLM call per group removes only
what the diff proves wrong, approving by default
(`R/internal/config/template/prompts/review_filter_task_system.md`).
Posting is a 3 437-line Node script for GitHub
(`R/scripts/github-actions/post-review-comments.js`) and example
scripts for GitLab, Gerrit, Codeup, GitFlic and Bitbucket
(`R/examples/`). There is no AST, no index, no call graph, no
confidence score, no owner command, no learning loop, no merge gate.

**repoach** reviews a pull or merge request through four one-shot
reviewers over one packed diff (`src/repoach/review/reviewer.py:54,634`,
`_DIFF_HARD_CAP_CHARS = 140_000`), turns every comment into a ledger
finding with a lifecycle (`src/repoach/review/findings.py`), verifies
what can be verified on disk (`finding_verifiers.py`), judges the rest
with an adversarial refuter on a heavier model whose evidence is the
whole cited file plus a tracked-file search (`refuter.py`,
`refuter_evidence.py`), re-verifies open findings at the merge head
(`head_reverify.py`), lets a maintainer settle a finding with
`/resolve` and `/reopen` (`owner_commands.py`), records each posted
finding's forge thread (`finding_posts.py`), and refuses the merge from
facts re-read at the head (`auto_merge.py`, `docs/merge_gate.md`).
GitHub and GitLab sit behind one `Forge` protocol
(`src/repoach/forge/`). The reviewers see only the diff and 30-line
excerpts around their own citations; the in-model read-only loop is
specified (`SP-AGENT-CONTEXT-LOOP`, status `draft`) and not landed.

## Capability table

Value is for repoach; effort is S (one module, a day), M (a few modules,
a week), L (crosses the orchestrator and the forges).

| Capability | open-code-review | repoach | Gap | Value | Effort | Spec |
|---|---|---|---|---|---|---|
| A comment names the code it is about, and its line is derived mechanically | `code_comment` requires `existing_code`; `ResolveComment` matches it against the hunk (whitespace-normalised), then the file, `RelocateAcrossFiles` across files, then an LLM `RE_LOCATION_TASK` (`R/internal/config/toolsconfig/tools.json:43-84`, `R/internal/diff/resolver.go:63,101,251`) | `ReviewComment` is `file, line, severity, body` (`src/repoach/review/reviewer.py`, class `ReviewComment`); the line is the model's word; the off-diff filter drops a comment whose *file* is not in the diff and keeps any line (`src/repoach/review/findings_bridge.py:562-603`); "Never comment on lines outside the diff" is prompt text (`prompts/review/sentinel_0.4.3.md`) | The one mechanical check OCR has that repoach lacks: a finding whose snippet is not in the diff is refused before it reaches the ledger; a right finding on a wrong line is moved, not lost | high | S–M | SP-FINDING-CITES-ITS-CODE |
| Per-path review rules, layered | 64 per-language checklists mapped by glob (`R/internal/config/rules/system_rules.json`, `rule_docs/`), overridden by `<repo>/.opencodereview/rule.json`, then `~/.opencodereview/rule.json`, then `--rule`; `merge_system_rule` keeps the built-in text under a project rule (`R/internal/config/rules/system_rules.go:206-216,299`) | One persona per role, versioned, repoach-specific: the Sentinel's scope names `MCP_*_ENABLED`, `mcp_tool_calls`, FastAPI `require_api_key` (`prompts/review/sentinel_0.4.3.md`, "You look for"); `[project].prompts_dir` is validated and the prompts are loaded from the package (`src/repoach/project/config.py:251`, `src/repoach/core/resources.py:19`); no per-path text | On a foreign repository the reviewers read repoach's own house rules; a Verilog file gets no Verilog checklist | high | M | SP-REVIEW-RULES-BY-PATH |
| Deterministic file selection with a named reason per exclusion | binary, secret path, user exclude/include, extension allow-list, default excludes (lockfiles, minified, `testdata`, and every test file), deleted, per-file token ceiling at 80 % of `MAX_TOKENS`; each file carries an `ExcludeReason` (`R/internal/agent/selection.go:14-70`, `R/internal/config/allowlist/`) | Whole files packed under 140 000 chars, the rest omitted and logged, not in the JSON (`src/repoach/review/diff_scoper.py`, `reviewer.py:634-641`); `[[review.generated]]` declares derived files and `always_reviewed` protects manifests (`src/repoach/project/always_reviewed.py`); no include/exclude, no lockfile or binary rule (`ReviewSection`, `src/repoach/project/config.py:437-445`) | Lockfiles and vendored code cost tokens and findings; what was not reviewed is not named in the output. Tests must stay reviewed (the Tester's object), so OCR's default is not taken | medium–high | S–M | SP-REVIEW-FILE-SELECTION |
| Large diffs reviewed in groups, nothing silently dropped | Groups of at most 10 files, LLM grouping at 4+ files, one bundle under 200 changed lines, one concurrent subtask per group with its own context (`R/internal/agent/grouping.go:22`, `template.go:130-140`, `agent.go:665-670`) | Files past the 140 000-char cap are not reviewed at all; the context-loop spec counts them as `context.truncated[]` but is a draft (`SP-AGENT-CONTEXT-LOOP` F3, G1) | A merge request larger than the cap is reviewed in part and reported as reviewed | high | L | SP-REVIEW-GROUPS-LARGE-DIFFS |
| Review a local range without a pull request; IDE and host-agent entry points | `ocr review` on the workspace, a commit or `--from/--to` (`R/cmd/opencodereview/shared_flags.go`); VSCode and JetBrains extensions (`R/extensions/`); skills and plugin manifests for Claude Code, Codex, Cursor, Kimi (`R/plugins/`, `R/skills/`); delegate mode where the host agent reviews and `ocr delegate` only selects files and rules (`R/skills/open-code-review-delegate/SKILL.md`) | `review pr <N>` needs a forge PR (`src/repoach/cli/review_cmds.py:262`); the bench already reviews a local base+patch pair through a read-only `LocalDiffForge` on a throw-away ledger (`src/repoach/bench/local_forge.py`, `bench/runner.py`) | A contributor or an agent cannot run the bench on its branch before pushing; the marketplace listings spec (`SP-MARKETPLACE-LISTINGS`) has no CLI to point at | high | M | SP-REVIEW-LOCAL-DIFF |
| A failed run resumes what finished | `--resume <id>` replays a session's JSONL and reuses every file whose diff fingerprint completed (`R/internal/session/resume.go:50-111`) | A run with an errored reviewer is never stored; the automatic retry reviews the head afresh, four reviewers again (`SP-REVIEW-SURVIVES-TRANSPORT-FAILURES` F10, NG3; `src/repoach/review/review_cache.py`) | The #288 incident paid three full reviews for one Sentinel failure | medium | M | SP-REVIEW-RESUMES-FAILED-ROLES |
| Machine-readable report formats | SARIF 2.1.0 (`R/cmd/opencodereview/sarif.go:27-28,131`), text and JSON; GitHub code scanning consumes SARIF | One JSON object on stdout (`SP-CLI-STDOUT-ONE-JSON`), the archive comment, `review report` | Findings cannot reach the GitLab MR code-quality widget or GitHub code scanning without a converter | medium | S | SP-REVIEW-REPORT-FORMATS |
| Token estimate before the run, run-wide budget | `--max-tokens-budget` with a pre-run estimate printed, per-group look-ahead, partial result exit 0 (`R/internal/agent/estimate.go`, `agent.go:683-711`) | Usage is recorded per call after the fact (`src/repoach/providers/usage.py`); the refuter's evidence is sized by `ceil(utf8_bytes / 3)` (`refuter_evidence.py:67`); no estimate before the first call, no run cap | A 300 k-token review is known after it was paid | medium | S | SP-REVIEW-TOKEN-ESTIMATE |
| Incremental review across pushes | `checkpoint_range`: the sticky summary records the reviewed head; the next run reviews `checkpoint..head`, fail-closed to the full range when the base moved, the config changed or ancestry cannot be proven (`R/action.yml:203-222`) | Every head reviews the whole diff; older findings are re-verified at the head (`head_reverify.py`); the replay cache answers only the same head (`review_cache.py`) | The second and later reviews of a long-lived MR re-read what was already judged | medium | M | SP-REVIEW-INCREMENTAL-HEAD |
| A published precision number on a public corpus | AACR-Bench, 200 PRs, 1 505 labelled issues, Apache-2.0; the README shows precision 33.9 %, recall 20.0 %, F1 25.1 % for OCR with Claude Opus, from an image, not reproducible from the repository (`R/README.md:53-69`, `R/imgs/benchmark-en.png`) | `repoach bench` on 10 planted cases, 0 labelled findings, `precision: cannot say` (`README.md` Precision block, `bench/corpus/README.md`) | The market table stake repoach named in `SP-PRECISION-MEASUREMENT` F5 now has a public corpus to be measured on, and a competitor's number to be read beside | high | M | SP-BENCH-PUBLIC-CORPUS |
| False-positive control after generation | One fact-checker call per group; removes a comment only when the diff proves it wrong (Ground A: the code is not in the file's diff; Ground B: a diff line contradicts it); approves by default; vetoes keep memory-safety, concurrency, compatibility and unused-parameter comments; `--no-filter` (`R/internal/config/template/prompts/review_filter_task_system.md`, `R/internal/agent/agent.go:1556-1608`) | Mechanical verifiers on disk, the refuter on a heavier model with the whole cited file and a tracked-file search, `cannot_judge` deferral when the evidence is outside the window, a hallucination guard with four observed modes (`finding_verifiers.py`, `refuter.py`, `refuter_evidence.py`, `hallucination_guard.py`) | None: repoach's is the stronger design. OCR's asymmetry rule ("removing a correct comment silently destroys a finding") is the rule repoach adopted after !324/!327 (`refuter.py`, module docstring) | — | — | — |
| Dedup | Review mode: prompt text "do not repeat" across rounds; scan mode: an LLM `DEDUP_TASK` from 4 comments per batch; across runs, an exact key `path|category|normalised snippet` (`R/internal/session/compare.go:87`) and a line-span IoU ≥ 0.6 on GitHub (`R/scripts/github-actions/post-review-comments.js:32`) | `finding_fingerprint` at a head, replay never re-posts (`findings_bridge.py`, `SP-POSTING-REPLAY-DEDUPES`) | None | — | — | — |
| Code understanding: AST, index, call graph | None: `git grep -F/-P` (100 hits, 10 s), `file_read` (500 lines), `file_find` (`R/internal/tool/`); no parser library in `R/go.mod` | None for the reviewers; Python `ast` for imports and verifiers (`src/repoach/review/import_gate.py`, `finding_verifiers.py`); `git grep -w` for the refuter's evidence | Neither has one; not a gap OCR opens | — | — | — |
| Reviewer tools (in-model loop) | Yes, every review is a tool loop, 100 tool calls max, memory compression at 60 % / 80 % of the context (`R/internal/llmloop/compression.go:20-29`) | Specified with a summarizer and path-jailed tools, not landed (`SP-AGENT-CONTEXT-LOOP`, `draft`) | Already a repoach spec; nothing added here | — | — | SP-AGENT-CONTEXT-LOOP (existing) |
| Owner commands, thread binding, merge gate, re-verification at head | None: no comment command (grep of `R/action.yml`, `R/scripts/`), no gate; `resolve_outdated` closes the action's own outdated threads with no human reply (`R/action.yml:153-170`) | `/resolve`, `/reopen`, `finding_posts`, open threads block the merge, the gate re-reads facts at the head | repoach ahead | — | — | — |
| Forges | GitHub Action packaged; GitLab, Gerrit, Codeup, GitFlic, Bitbucket as example scripts; no Gitee found | GitHub and GitLab behind one protocol, conformance-tested | repoach ahead on GitLab; OCR wider on paper | — | — | — |
| Model choice | 30 presets, 4 wire protocols, one model for every phase (`R/internal/llm/providers.go`, `protocol.go:22-37`, `agent.go:114-118`) | Three tiers per role (opus/sonnet/haiku), presets, gateway with failover | repoach ahead | — | — | — |
| Prompt caching | `cache_control` breakpoints, `prompt_cache_key`, rule blocks sorted for a stable prefix (`R/internal/llm/client.go:1643-1647`, `agent.go:1686-1693`) | 95 % cached on the Claude Code route (README Status) | None measurable here | — | — | — |
| Learning from humans | None; "domain-specific long-term memory" planned H1 2027 (`R/ROADMAP.md`) | `finding_fate`, calibration cells, opt-in memory (`SP-UNIVERSAL-LEDGER`, `SP-MEMORY-LOOPS-EXPLICIT`) | repoach ahead | — | — | — |

## What repoach does not take, and why

- **Excluding test files by default** (`R/internal/config/allowlist/default_exclude_patterns.json:2-9`). The Tester reviews tests and the spec gate counts them; `SP-REVIEW-FILE-SELECTION` names tests as always reviewed.
- **One model for every phase.** repoach's tiers and the refuter on a heavier model are the design that caught 8 wrong verdicts in 13.
- **Resolving outdated threads automatically.** An open thread blocks the merge by owner rule (`SP-OPEN-THREADS-BLOCK-THE-MERGE`); a thread is closed by its role or the owner (`SP-PR-THREADS-REMEMBER-AND-CLOSE`, draft), never because a line moved.
- **An LLM relocation call.** `SP-FINDING-CITES-ITS-CODE` relocates by snippet match only; a snippet found nowhere refuses the finding with a reason instead of asking a model where it meant.
- **Prompt-only "do not repeat" dedup.** The fingerprint already does it.

## Order

`docs/specs/2026-11-12_SP-OCR-GAP-PROGRAMME_what-repoach-takes-from-open-code-review.md`
holds the dependency order, the owner decisions and the gauge.
