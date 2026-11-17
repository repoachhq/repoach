---
id: SP-REVIEW-REMEMBERS-EVERY-ROUND
title: The reviewer remembers every round — all prior threads, every reply whoever wrote it, and their resolution state reach the next round's prompt
version: 0.4
status: approved
author: agent
created: 2026-11-15
updated: 2026-11-17

owns:
  code: [src/repoach/review/review_history.py, prompts/review/_review_history_0.1.0.md, tests/unit/test_review_history.py, tests/unit/test_review_history_in_prompt.py]
  resources: [format:review-history-block]

depends_on: [SP-FINDING-MODEL, SP-FINDING-THREAD-IDS, SP-OWNER-SETTLES-A-FINDING, SP-EVIDENCE-SENTINEL-AUTHOR, SP-PROMPT-PLACEHOLDER-ORDER, SP-REFUTER-INJECTION-HARDEN, SP-ORCH-DOCSTRING, SP-UNIVERSAL-FORGE, SP-FINDING-CITES-ITS-CODE, SP-REVIEW-RESUMES-FAILED-ROLES]
provides_to: []

constraints:
  history_block_max_tokens: 12000
  reply_text_max_chars: 600
  claim_text_max_chars: 400
  register_line_max_chars: 240
---

# The reviewer remembers every round

## Amendment (0.4, 2026-11-17): the memory is built from tables that exist

Re-measured on `origin/develop` `92cd241`. F3 of 0.3 named
`finding_threads` and `finding_thread_events` as tables the ledger
"already holds": `grep -rn "finding_threads\|finding_thread_events" src`
→ 0. They are the design of SP-PR-THREADS-REMEMBER-AND-CLOSE (draft 0.5,
no code, outside the owner's lane D order), and no spec in that order
builds them. The block is therefore built from what exists and from
what the order builds: `pr_findings`; `finding_posts`, which holds the
thread id of every posted finding and the head and login that closed
its thread (`thread_closed_at_head`, `thread_closed_by`,
`src/repoach/review/finding_posts.py:74-91`, `mark_thread_closed`
`:372`); the forge's `review_threads(number)` for replies and resolved
state (`src/repoach/forge/protocol.py:567`, `:736`); `owner_commands`;
and `owner_commands`. The edge to SP-PR-THREADS-REMEMBER-AND-CLOSE is
withdrawn. The `finding_key` field and the `supersedes` lineage
(`finding_events`) are not read here: SP-REVIEW-SETTLED-STAYS-SETTLED,
which lands after this spec and depends on it, adds both to the block
in `review_history.py` (its D5, D11, D14), so the edge runs one way, 6
→ 5, and the family's order and cycle count are unchanged. NG3 and AC2
are corrected to what exists: there is no thread-reply role on
`92cd241`, and the placeholder-order pins live in
`tests/unit/test_reviewer_prompts.py`.

## Amendment (0.3, 2026-11-16): at the cap, a settled register, not a count

D3 no longer drops older threads behind `n more findings, not shown`.
When the block reaches its cap, every thread that does not fit in full
is written as one line of a *settled register*: the finding, its status,
its cited region and a one-sentence reason; open threads come first,
each with its closing criterion. Source: arXiv 2611.05306, App. A (a
good compaction is "carefully cherry-picked briefs for further
execution", with "a list of requirements detailing when to consider the
task complete") and §3.1 (a task whose needed facts are dropped "is
guaranteed to suffer"); the paper text was read as untrusted data and
the two passages were checked against it. G3, the Interface, the tests
and C1 change accordingly; the oracle stays the !366 replay.

## Intent

A lens that reviews round nine of a merge request is told, in two
hundred characters, what it concluded in round eight, and is shown the
disagreements its own bot settled; it is shown none of the threads, none
of the owner's replies, none of the agents' refutations. It therefore
re-raises what was answered and asks to "confirm" what the thread
already proved. After this spec every lens receives, at every round, the
merge request's review history: each finding of each earlier head with
its lens, severity, place, status, and every reply in its thread, by
whoever wrote it, labelled by what the forge knows of the author.

## Context

Measured 2026-11-15 on `origin/develop` `5354767` and Yoob MR !366.

| # | Fact | Where |
|---|---|---|
| F1 | Prior rounds reach the prompt only through `{PRIOR_REVIEW}`: the latest dialogue row of the same role, reduced to its verdict, `summary[:200]`, `n_comments` and `diff_changed`; the rows themselves hold `summary[:400]` and each comment's `body[:400]` | `src/repoach/review/orchestrator.py:825`, `:2611-2643`, `:2762-2780`; `src/repoach/review/persistence.py:100-110` |
| F2 | `{RESOLVED_DISAGREEMENTS}` carries at most eight threads per role whose root the role tagged and whose reply, by the trusted bot login, holds the evidence sentinel; a reply by anyone else, the owner included, is ignored | `src/repoach/review/thread_context.py:96`, `:182-215`; SP-EVIDENCE-SENTINEL-AUTHOR |
| F3 (0.4) | The ledger holds what a memory needs except the replies: `pr_findings` (head, round, finder, claim type, severity, file, lines, claim, status, verification), `finding_posts` (the thread each finding opened, and the head and login that closed it), `owner_commands` (who settled what, with the reason); the replies themselves, with author login and date, and a thread's resolved state are read from the forge (`review_threads`). `finding_threads` and `finding_thread_events` do not exist on `92cd241` (`grep -rn … src` → 0) and no spec of the order builds them | `src/repoach/review/findings.py:269-291`; `src/repoach/review/finding_posts.py:74-91`, `:372`; `src/repoach/forge/protocol.py:567`, `:736`; SP-FINDING-THREAD-IDS; SP-OWNER-SETTLES-A-FINDING |
| F4 | On !366, round 12: of 8 threads, 2 re-raised points settled in round 11 with a measurement, 3 asked to confirm tests whose `file:line` a reply had already given; 5 of 8 were the reviewer's window, not the branch | Yoob `tmp/REVIEW-SPEC.md` §4–§6 |
| F5 | Owner decision 2026-11-15 (a): the reviewer remembers every round — all prior threads, plus every reply and refutation whoever wrote it (owner, agents, bot), with their resolution state | brief, 2026-11-15 |
| F6 | Untrusted text must not be able to expand a placeholder or break a fence: placeholders are expanded before the diff is substituted, and the refuter's evidence window is fence-hardened | SP-PROMPT-PLACEHOLDER-ORDER; SP-REFUTER-INJECTION-HARDEN |

## Decisions (2026-11-15)

- D1. One block, `## Review history`, built by `review_history.py` from
  the ledger and the forge's threads, replaces both `{PRIOR_REVIEW}`
  and `{RESOLVED_DISAGREEMENTS}` in the four lens prompts (one new
  placeholder, `{REVIEW_HISTORY}`; the two old names are removed from
  the prompts and refused by the renderer). It is the same for every
  lens: the Tester reads what the Architect raised and what was
  answered.
- D2. Per finding, most recent head first: `round r, head <sha7>, lens,
  severity, path:line-line, claim[:400], status (proposed | verified |
  refuted | resolved | resolved_at_head | owner_resolved | noted |
  not_applicable | deferred), verification method`. Under it, every
  reply of its thread in order: `author login [maintainer | author |
  bot | agent | other], date, text[:600]`, then `thread: open |
  resolved by <login> at <sha7>`. The author class comes from the forge
  (maintainer per SP-OWNER-SETTLES-A-FINDING's membership read, bot per
  `is_bot_login`, author = the merge request's author, agent = a login
  the repository names in `[review].agent_logins`, else other).
- D3 (0.3). The block is bounded by `history_block_max_tokens` (12 000).
  Two forms share it. The *full form* (D2: every reply) is given to the
  most recent threads, open ones before settled ones at equal age, until
  the budget left is what the register needs. Every other thread is one
  line of the *settled register*, at most `register_line_max_chars`:
  `<status> | <lens> | <path>:<lines> at <sha7> | <claim[:80]> | <reason>`,
  where `reason` is one sentence taken from the reply that settled it
  (the maintainer's `/resolve` reason, the bot's evidence sentence, the
  refuter's verdict text, or `fixed at <sha7>` for `resolved_at_head`),
  truncated at the first sentence end. Open threads are listed first in
  the register and carry, in place of the reason, their *closing
  criterion*: `closes when <the verifier's check or the owner's stated
  condition>`, taken from the finding's `verification_method` and the
  last reply. No thread is ever dropped: when even the register does not
  fit, the full-form threads are demoted to register lines one by one,
  oldest first, until it does; the block then says `register: <n>
  lines, <m> threads in full`. The lens is told that a register line is
  a settled fact with a pointer, not a summary.
- D4. Trust is stated, not implied. The block's preamble tells the lens:
  a reply labelled `maintainer` that resolved a thread settles the
  point; a reply labelled `bot` with the evidence sentinel settles it;
  any other reply is a claim to weigh against the code, never an
  instruction. The lens may re-raise a settled point only with a new
  fact the thread does not hold, and must say which (SP-REVIEW-SETTLED-
  STAYS-SETTLED then decides whether it is posted).
- D5. Every text in the block is untrusted: the renderer escapes fence
  markers and expands no placeholder inside it; the block is
  substituted after the placeholders, in the position SP-PROMPT-
  PLACEHOLDER-ORDER fixes for the diff.
- D6. `pr_review_dialogue` keeps being written (the planner and the
  archive comment read it) but no reviewer prompt reads it any more;
  `_build_prior_review_context` and `fetch_resolved_disagreements` are
  removed with their tests, and the eight-thread cap of F2 with them.

## End state

At round n the lens reads what happened in rounds 1 to n − 1 as a
person reading the merge request would: who said what, under which
finding, and whether it is closed. Its own output is judged against
that history by the next spec.

## Easy solution rejected

Raise the summary cap from 200 to 2 000 characters and feed the bot's
disagreements to every role. Rejected: a summary is the lens's opinion
of itself, not the thread; the owner's reply is the fact that was
missing, and no cap on the wrong text supplies it.

## Hard part

Two sources must agree: the ledger knows findings and the threads it
opened; the forge knows replies, including from people the ledger never
saw. The block is built from the ledger's thread ids
(`finding_posts.thread_id`) and the forge's thread listing joined on
that id; a thread the forge no longer returns is shown as `thread:
unreadable` with its last known state, never dropped, and a reply by a
login the forge cannot classify is `other`, never promoted. The token
budget is the second hard part: a long merge request outgrows 12 000
tokens by round 6, so the cap must favour open and recent threads and
say what it dropped.

## Goals

- G1: one `{REVIEW_HISTORY}` block per round, identical across the four lenses, with the layout of D2 and the preamble of D4.
- G2: every reply of every thread repoach opened on the merge request is in the block or counted in a dropped line; no reply is filtered by its author.
- G3 (0.3): the block never exceeds the cap and never drops a thread: every thread is in full form or is one register line with its status, region, reason or closing criterion; open threads lead the register.
- G4: the two old placeholders are gone from the prompts and the renderer refuses them; `PriorReviewContext`, `_build_prior_review_context`, `fetch_resolved_disagreements` and `_MAX_DISAGREEMENTS_PER_ROLE` are deleted.
- G5: the block is fence-escaped and substituted after placeholder expansion.

## Non-Goals

- NG1: no decision here on what is posted (SP-REVIEW-SETTLED-STAYS-SETTLED).
- NG2: no cross-merge-request memory (SP-REVIEW-MEMORY-STORE).
- NG3 (0.4): no thread-reply agent; none exists on `92cd241`, and SP-PR-THREADS-REMEMBER-AND-CLOSE (draft, unscheduled) would add one. The block is read by the four lenses only.

## Assumptions

- A1: `finding_posts` holds a thread id for every finding posted since SP-FINDING-THREAD-IDS landed; older findings are shown from the ledger alone with `thread: unknown`.
- A2: the forge listing of threads returns the replies' author logins and dates on both forges (SP-UNIVERSAL-FORGE `review_threads`).

## Interface

```python
# src/repoach/review/review_history.py
class HistoryReply(BaseModel):
    login: str; author_class: Literal["maintainer", "author", "bot", "agent", "other"]
    at: datetime; text: str              # [:600]

class HistoryFinding(BaseModel):
    round: int; head_sha: str; lens: str; severity: str
    path: str; line_start: int; line_end: int
    claim: str; status: str; verification_method: str
    replies: list[HistoryReply]
    thread_state: Literal["open", "resolved", "unreadable", "unknown"]
    resolved_by: str | None; resolved_at_head: str | None

class RegisterLine(BaseModel):
    status: str; lens: str; path: str; line_start: int; line_end: int; head_sha: str
    claim: str                            # [:80]
    reason: str | None                    # one sentence, settled threads
    closing_criterion: str | None         # open threads

class ReviewHistory(BaseModel):
    findings: list[HistoryFinding]        # full form, most recent first
    register: list[RegisterLine]          # open first, then settled, most recent first
    tokens: int

def register_line(finding: HistoryFinding) -> RegisterLine: ...     # the reason or the closing criterion, one sentence

def build_review_history(forge: Forge, pr_number: int, *, db_path: Path, head_sha: str, max_tokens: int = 12000) -> ReviewHistory: ...
def render_review_history(history: ReviewHistory) -> str: ...     # format:review-history-block, fence-escaped

# reviewer.py (changed): placeholder {REVIEW_HISTORY}; {PRIOR_REVIEW} and {RESOLVED_DISAGREEMENTS} refused
# prompts/review/{architect,sentinel,tester,scribe}: the two placeholders replaced by {REVIEW_HISTORY}; versions bumped
# to architect 0.7.0, sentinel 0.6.0, tester 0.6.0, scribe 0.7.0 — the second bump of the owner's unified order
# (2026-11-16), after SP-FINDING-CITES-ITS-CODE's (0.6.0 / 0.5.0 / 0.5.0 / 0.6.0) and after SP-REVIEW-RESUMES-FAILED-ROLES,
# whose reuse key carries persona_version and therefore must exist before either bump
```

Outputs: the block; review JSON key `history: {findings, replies, register, tokens}`; log event `review_history_built`.

## Behavior

### Nominal
Round 3: 11 findings over two heads, 7 settled, 4 open, 23 replies; the
block lists them most recent first, each reply labelled; 3 100 tokens.

### Edge cases
- first round → the block is `## Review history\n(none: first review of this merge request)`.
- a thread resolved by a maintainer with `/resolve` → `owner_resolved`, the reason text as a reply labelled `maintainer`.
- a reply by the merge request's author who is also a maintainer → `maintainer` (the stronger class).
- the cap is hit → the oldest settled threads become register lines first, then older open ones keep their closing criterion at the head of the register; the JSON says how many of each form.
- a settled thread whose settling reply has no sentence (a bare `/resolve`) → `reason: settled by <login>, no reason given`.

### Failure scenarios
- the forge's thread listing fails → every finding's thread is `unreadable`, the ledger's last known state is shown, the log names the error; the review proceeds.

## Tests

| Test | What it pins | Mutation that must turn it red |
|---|---|---|
| `tests/unit/test_review_history.py::test_every_reply_is_shown_whoever_wrote_it` | owner, author, agent, bot replies all present with their class | filter non-bot replies |
| `::test_author_classes_from_the_forge` | D2 classes on fakes | label everyone `other` |
| `::test_cap_turns_older_threads_into_register_lines_open_first` | D3: with a cap that fits 6 of 20 threads in full, the other 14 are register lines, open ones first with a closing criterion, settled ones with a one-sentence reason; no thread absent | count instead of listing |
| `::test_register_line_reason_is_the_settling_sentence` | the `/resolve` reason, the bot's evidence sentence, the refuter's text, `fixed at <sha7>` | repeat the claim as the reason |
| `::test_register_never_drops_a_thread_under_any_cap` | cap just above the register's own size: every thread is a line | drop the oldest |
| `::test_fence_markers_are_escaped_and_placeholders_not_expanded` | a reply holding ```` ``` ```` and `{DIFF}` | substitute raw |
| `::test_unreadable_thread_is_shown_not_dropped` | failure scenario | drop |
| `tests/unit/test_review_history_in_prompt.py::test_four_prompts_carry_review_history_and_not_the_old_placeholders` | G4 | leave `{PRIOR_REVIEW}` |
| `::test_block_is_identical_across_lenses` | D1 | per-role filtering |
| `::test_old_builders_are_gone` | `_build_prior_review_context` absent | — |

## Success conditions

- C1: replaying !366's recorded rounds 1–11 (findings, replies, resolutions as the Yoob `REVIEW-SPEC.md` and the MR hold them) builds, for round 12, a block under 12 000 tokens that contains the round-11 measured replies to `c56ed3f7` and `e3555287` verbatim within 600 characters, and one register line for every thread of rounds 1–8 (45 threads by F4's counts) with its status and reason; oracle: the test asserts the two reply texts, the `maintainer` label, and `len(findings) + len(register) == 63` (the threads of rounds 1–11).
- C2: on a live Yoob merge request after this lands, `jq .history.replies review-gate.json` equals the number of replies on the MR's repoach threads as the GitLab API lists them; oracle: `glab api` count.
- C3: `grep -rn "PRIOR_REVIEW\|RESOLVED_DISAGREEMENTS" src prompts` → 0.

## Architecture Impact
- Adds dependency: SP-REVIEW-REMEMBERS-EVERY-ROUND -> SP-FINDING-MODEL, SP-FINDING-THREAD-IDS, SP-OWNER-SETTLES-A-FINDING (reads `pr_findings`, `finding_posts` with its closure columns, `owner_commands`; 0.4: the edge to SP-PR-THREADS-REMEMBER-AND-CLOSE is withdrawn, its tables do not exist and are not built by the order)
- No edge to SP-REVIEW-SETTLED-STAYS-SETTLED (0.4): that spec depends on this one and adds `finding_key` and the `supersedes` lineage to the block when it lands; an edge the other way would be a cycle.
- Adds dependency: SP-REVIEW-REMEMBERS-EVERY-ROUND -> SP-EVIDENCE-SENTINEL-AUTHOR (the bot's sentinel keeps its settling meaning inside the block; the bot-only filter is retired)
- Adds dependency: SP-REVIEW-REMEMBERS-EVERY-ROUND -> SP-PROMPT-PLACEHOLDER-ORDER, SP-REFUTER-INJECTION-HARDEN (untrusted text position and escaping)
- Adds dependency: SP-REVIEW-REMEMBERS-EVERY-ROUND -> SP-ORCH-DOCSTRING, SP-UNIVERSAL-FORGE (the orchestrator's step list changes; threads are read through the protocol)
- Adds dependency (0.3, order): SP-REVIEW-REMEMBERS-EVERY-ROUND -> SP-FINDING-CITES-ITS-CODE (the persona files this spec bumps are the ones that spec created at 0.6.0 / 0.5.0 / 0.5.0 / 0.6.0; the history block's `path:line` are anchored lines), SP-REVIEW-RESUMES-FAILED-ROLES (its reuse key names `persona_version`; a bump before it exists would be a key it never saw)
- Removes: `thread_context.fetch_resolved_disagreements`, `orchestrator._build_prior_review_context`. No table, no cycle.

## Diagram
```mermaid
flowchart LR
    L[pr_findings + finding_posts + owner_commands] --> J[join on thread id]
    F[forge threads and replies] --> J
    J --> C[classify authors, cap by tokens]
    C --> R[render, escape] --> P[{REVIEW_HISTORY} in the four prompts]
```

## Acceptance Criteria
- [ ] AC1: `pytest -q tests/unit/test_review_history.py tests/unit/test_review_history_in_prompt.py` → green. Today (`5354767`): the files do not exist; `fetch_resolved_disagreements` ignores every non-bot reply (`thread_context.py:187-195`).
- [ ] AC2: `pytest -q tests/unit/test_thread_context*.py tests/unit/test_reviewer_prompts.py` → green after the removal of G4 (their resolved-disagreement cases deleted with the function; 0.4: `test_reviewer_prompts.py` carries the SP-PROMPT-PLACEHOLDER-ORDER pins, `test_untrusted_diff_tokens_not_expanded` and `test_coder_and_developer_render_inject_untrusted_last`; no `test_prompt_placeholder_order.py` exists).
- [ ] AC3: `repoach arch check --base origin/develop` → `edge-honesty: ok`.
- [ ] AC4: `docs/review_redesign_architecture.md` names the block where it names `{PRIOR_REVIEW}` today.

## Open Questions
- None. Decided by the owner on 2026-11-15: Q1 (who is an `agent`) — the logins of `[review].agent_logins` in `repoach.toml`; on Yoob the agents post as the maintainer and are classed so; Q2 (history cap) — fixed at 12 000 tokens, named in the block.
