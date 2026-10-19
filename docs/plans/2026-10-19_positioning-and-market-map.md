# Positioning and market map — where repoach sits among AI code-review and autonomous-development tools

Assembled 2026-10-19 from vendor pages read that day (sources at the
end). Prices are list prices as published on the vendor page on
2026-10-19 unless marked otherwise; a figure marked **unverified** was
not read on a vendor page and comes from a secondary source or a
derivation shown in the text. Every number from the owner conversation
of 2026-10-19 is kept; nothing is added without a source.

Legend for the map: *loop* is where the product sits in the change
lifecycle (review comments, review + fix, autonomous development,
specification); *runs* is where it executes (vendor SaaS, the user's
CI, a local CLI); *model policy* says who chooses and pays for the
model; *does not* is the one thing the vendor itself states it does not
do, or that the page does not offer. `n/m` = not measured on 2026-10-19.

## 1. Market map

| Player | Loop | Runs | Forges | Model policy | Price (list, 2026-10-19) | OSS | Does not |
|---|---|---|---|---|---|---|---|
| GitHub Copilot code review | review comments | GitHub SaaS | GitHub | bundled | $0.05–5 per review, bundled in Copilot plans | no | never a blocking gate |
| Claude Code review (Anthropic) | review comments | Anthropic SaaS / GitHub app | GitHub | bundled, usage-billed | per review, see `code-review-costs` page | no | "never approves or blocks" (vendor wording) |
| OpenAI Codex review | review + fix | OpenAI SaaS | GitHub only | bundled | n/m | no | GitLab |
| Cursor Bugbot | review comments | Cursor SaaS | 4 forges | bundled | ≈ $1–1.5 per run | no | a merge gate |
| Devin Review (Cognition) | review comments | Cognition SaaS | n/m | bundled | n/m | no | "cannot block merges" (vendor wording) |
| CodeRabbit | review + fix suggestions | CodeRabbit SaaS | GitHub, GitLab | vendor's models, no BYOK | $24–72 per developer per month | no | bring-your-own key |
| Qodo PR-Agent | review + fix suggestions | the user's CI or Qodo SaaS | GitHub, GitLab | any model, BYOK | ≈ $0.02–0.25 per review on a key | MIT | an evidence-verified gate |
| Greptile | review comments | Greptile SaaS | n/m | bundled | $30 per seat per month | no | n/m |
| Graphite (Cursor-owned) | review + stacked PRs | Graphite SaaS | GitHub only | bundled | see pricing page | no | GitLab |
| OpenHands | autonomous development | self-host or SaaS | n/m | BYOK | see pricing page | MIT | a merge gate |
| Aider | local coding assistant | local CLI | any (git) | BYOK | free | OSS | maintained: maintenance mode |
| Jules (Google) | autonomous development | Google SaaS | GitHub | Gemini, free tier | free | no | GitLab |
| GitLab Duo Code Review | review comments | GitLab SaaS / self-managed | GitLab | bundled | $0.25 per review, flat (announced 2026-03) | no | a blocking gate |
| Ellipsis | review + fix | Ellipsis SaaS | n/m | bundled | see pricing page | no | n/m |
| Sourcery | review comments | Sourcery SaaS | n/m | bundled | see pricing page | no | n/m |
| Kiro (AWS) | specification → code | IDE | — | bundled | n/m | no | stops before the PR |
| Spec Kit (GitHub) | specification → plan | local CLI | — | any agent | free | OSS | stops before the PR |
| Tessl | specification registry | SaaS / CLI | — | any agent | n/m | partly | stops before the PR |

Category size: ≈ $420 M ARR across the AI code-review segment
(**unverified**: an analyst aggregate quoted in the conversation, not a
vendor figure).

Reading the map by column:

- **Loop.** Twelve players sit at "review comments" or "review +
  fix"; three sit before the PR (specification); two are autonomous
  developers; Aider is a local assistant. Nobody but repoach joins the three: a governed spec in the
  repository, a planner and developer that must satisfy it, and a review
  bench whose findings are re-verified at merge time.
- **Runs.** Only PR-Agent, OpenHands and Aider run on the user's
  infrastructure without a vendor account; PR-Agent is the only one of
  those that reviews PRs in the user's CI, which is how it got adopted.
- **Forges.** GitHub is served by everyone; GitLab by Duo, Bugbot,
  CodeRabbit and PR-Agent (the four the wedge in section 7 names). None
  of the GitLab options blocks a merge on evidence.
- **Model policy.** Bundled models dominate; BYOK exists at PR-Agent,
  OpenHands, Aider — none of them with a gate.
- **Does not.** Three vendors state in their own words that they do not
  block: Copilot, Claude Code review, Devin. The others simply do not
  offer it.

## 2. What only repoach has

| Capability | Where it lives | Who else has it |
|---|---|---|
| A governed spec committed in the repository, with ownership and dependency frontmatter, that gates the plan (`repoach arch check`, edge honesty) | `docs/specs/`, `src/repoach/arch/`, `src/repoach/lint/edge_honesty.py` | nobody (spec tools stop before the PR) |
| A merge gate that re-verifies every finding at the exact head it is about to merge and refuses an LGTM | `src/repoach/review/merge_gate.py`, `review gate` | nobody (vendors state they never block) |
| An adversarial refuter on a heavier model that must disprove a finding before it is believed | `src/repoach/review/refuter.py` | nobody published |
| Bring-your-own key, self-hosted, with the gate | `SP-UNIVERSAL-PROVIDERS`, `SP-UNIVERSAL-INSTALL` | BYOK exists (PR-Agent, OpenHands) without a gate |

## 3. Table stakes repoach is missing

Measured on `develop` `20dbb45` (2026-10-18) and on the W1 run; each
line names the spec that closes it.

| Missing | Evidence | Closed by |
|---|---|---|
| One-command install | `repoach version` fails without a gateway URL (umbrella A1) | `SP-UNIVERSAL-INSTALL` |
| Non-Python repositories | AST verifiers on `.ts` files (umbrella A8) | `SP-UNIVERSAL-GATES` |
| GitLab | nineteen `gh` methods, no GitLab path (umbrella A3) | `SP-UNIVERSAL-FORGE` |
| A hosted trial or free-for-public tier | none | not before a measured false-positive rate (section 5) |
| A UI on the ledger | SQLite only, `review insights` on the CLI | `serve` stage (section 5), not specified |
| A published precision / false-positive number | 225 582 tokens per review, 0 tool calls, FP rate unmeasured (W1) — unsellable against "95.9 % precision" headlines (**unverified**: a competitor's claim quoted in the conversation, not re-read on 2026-10-19) | `SP-UNIVERSAL-LEDGER` (finding fate from the forge) |
| Working free model chains | every NIM tier HTTP 410 since August; OpenRouter credit negative (production plan § 1.2) | `SP-UNIVERSAL-PROVIDERS` (a key, not a chain) |

## 4. Cost per review

Measured: one review on Yoob MR !283 (36 files, 195 958 diff
characters) consumed 225 582 tokens over 7 gateway calls in 83 s, with
0 tool calls (W1 report). Derived prices, from list prices of 2026-06
(**unverified** on 2026-10-19 — re-read the price page before quoting):

| Model class | Cost per review at 225 k tokens |
|---|---|
| Sonnet 5 | ≈ $0.5–0.7 |
| Opus 5 | ≈ $1.2–1.7 |
| Fable 5.1 | ≈ $2.4–3.4 |

Yoob merges ≈ 3.3 MR per day (98 in 30 days, production plan § 3) →
≈ $25–170 per month on a key, depending on the class.

Market band: $0.25–5 per review, or $12–48 per seat per month. Repoach
is inside the band only on a Sonnet-class model; the four-reviewer
fan-out on Opus or Fable sits at the top of the band or above it. The
ledger spec's cost signal (tokens and wall time per review by diff
size) is what decides where to cut the fan-out and which model
suffices; without it the choice is a guess.

## 5. Deployment order

1. **Your-CI job first.** `pipx install repoach && repoach review pr --dry-run` from GitHub Actions or GitLab CI: no server, no uptime SPOF, the secret is a CI variable. This is how PR-Agent was adopted, and it is what `SP-UNIVERSAL-CI-ENTRY` ships as a snippet. Yoob gets a GitLab `review:advisory` job with an Anthropic key as a masked variable.
2. **Local CLI** — the same command from a shell; it falls out of step 1 with nothing extra.
3. **`repoach serve` later** — webhooks and a ledger UI, for teams that want a long-running process; not before the first two have external users.
4. **SaaS last** — not before a measured false-positive rate and 20+ external repositories. Hosting first would mean competing on CodeRabbit's terms alone.

Repoach's own repository dogfoods the install path; the two runners on
the owner's PC are not the product.

## 6. Positioning sentences

- "The merge gate that trusts evidence, not the model."
- "Governed specs, an autonomous developer that must satisfy them, reviewers refuted before they are believed."
- "Bring your forge, your CI, your key."

## 7. Wedge

Small teams on GitLab self-managed or CE, and regulated-adjacent shops
(hardware, medical, fintech) that already run a CI barrier and want a
gate they can audit. GitHub users are saturated by native review;
GitLab users have Duo, Bugbot, CodeRabbit and PR-Agent, none of which
blocks on evidence. Indie maintainers are skipped until a free hosted
tier exists.

## 8. Risks

| Risk | Consequence | What survives it |
|---|---|---|
| Forge-native review commoditised to ≈ $0 by 2027 | review comments alone are worth nothing | the gate and the governed spec |
| Model quota or price shocks multiplied by the four-reviewer fan-out | cost per review leaves the band | the ledger's cost signal picks the fan-out and the model class |
| Forge API and bot-identity churn | posting and trust break per forge | the `Forge` protocol and its conformance suite |
| A benchmark war without a published precision number | every claim is unverifiable | the fate signal measured against human decisions |
| Spec frontmatter made mandatory | the five-minute universality gauge fails on a foreign repository | `owns` optional (`SP-UNIVERSAL-GATES`), `spec: null` tolerated (`SP-UNIVERSAL-PROJECT-CONFIG` G4) |
| Bus factor of one | nothing ships when the owner stops | the factory builds from specs; this document and the specs are the hand-over |

## 9. Sources (read 2026-10-19)

| Vendor | Page |
|---|---|
| GitHub Copilot code review | `docs.github.com` — Copilot code review |
| Claude Code review | `code.claude.com` — code-review-costs |
| GitLab Duo Code Review | `about.gitlab.com` — agentic code reviews with flat-rate pricing (2026-03) |
| Cursor Bugbot | `cursor.com/docs/bugbot` |
| Devin Review | `docs.devin.ai` — devin-review |
| CodeRabbit | `coderabbit.ai/pricing` |
| Qodo PR-Agent | `qodo.ai/pricing` |
| Greptile | `greptile.com/pricing` |
| Graphite | `graphite.com/pricing` |
| OpenHands | `openhands.dev/pricing` |
| Ellipsis | `ellipsis.dev/pricing` |
| Sourcery | `sourcery.ai/pricing` |

Codex, Aider, Jules, Kiro, Spec Kit and Tessl were characterised from
their public repositories or product pages without a pricing page read
that day; their rows carry `n/m` where no figure was read. Internal
evidence: `docs/plans/2026-10-18_yoob-production-plan.md` (runtime and
Yoob throughput), `docs/plans/2026-10-18_W1-gitlab-dry-run-measurement.md`
on branch `feat/sp-gh-glab-shim-dry-run` (tokens, wall time, tool calls),
`docs/specs/2026-10-18_SP-UNIVERSAL-REPO_repoach-runs-on-any-repository.md`
(the measured assumptions A1–A13).
