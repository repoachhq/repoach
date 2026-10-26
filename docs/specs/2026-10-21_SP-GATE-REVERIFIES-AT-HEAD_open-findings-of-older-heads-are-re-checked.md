---
id: SP-GATE-REVERIFIES-AT-HEAD
title: The gate re-verifies every open blocking finding of an older head at the head it decides on
version: 0.3
status: approved
author: agent
created: 2026-10-21
updated: 2026-10-25

owns:
  code: [src/repoach/review/head_reverify.py, src/repoach/review/finding_verifiers.py, tests/unit/test_gate_reverifies_at_head.py, tests/unit/test_spec_gate_vitest_body.py, tests/unit/test_review_pr_reverifies_at_head.py, tests/unit/test_finding_verifiers.py, tests/integration/test_gate_reverifies_at_head_end_to_end.py]
  resources: []

depends_on: [SP-FINDINGS-INIT-RACE, SP-REFUTER-JUDGES-WHAT-IT-CAN, SP-UNIVERSAL-GATES, SP-REVIEW-PINS-HEAD, SP-DEDUP-CLASSIFICATION-CONSTANTS, SP-PLAN-SELECTOR-AUDIT-WIRE, SP-PLAN-CONTRACT-LINTS, SP-UNIVERSAL-FORGE, SP-ORCH-DOCSTRING, SP-RETRY-BACKOFF-DEDUP]
provides_to: []

constraints: {}
---

# The gate re-verifies open findings of older heads at the head

## Amendment (0.3, 2026-10-25): a required Python test is found by name at the head

Measured 2026-10-25 on Yoob MR !324, head `910a6cac` (`review:gate`
job 17451911364): finding 6, `missing_test` on
`api/alembic/versions/n0g1e2n3r4c5_relabel_generic_board.py:58` raised
at `606ebe3`, was reported `still present at 910a6ca (no test for
test_migration_relabels_generic_rows_and_downgrade_restores_only_noted_rows)`
while `git grep` at that head finds the test at
`api/tests/test_relabel_generic_board_migration.py:95`. It alone kept
`REQUEST_CHANGES`. Cause: the Python `missing_test` verifier
(`finding_verifiers._verify_missing_test`) searched only the top-level
`tests/` directory (`make_tests_symbol_searcher`), so a monorepo's
`api/tests/` was never read; the head tree and the recorded file were
not at fault. A `still present` stamp left at the head by that narrower
check would also have been reused by every later gate at the same head.

- G11: the re-check of a Python `missing_test` finds the covering test
  by name across every Python test file of the pinned head tree (the
  tracked files of the head worktree: a configured test root, any
  `tests/` or `test/` directory, `test_*.py`, `*_test.py`,
  `conftest.py`); code under review never counts
  (SP-MISSING-TEST-VERIFIER-SCOPE holds). A found test resolves the
  finding with `<test> exists at <path>:<line>`; none keeps it open with
  `no test for <names> (searched <n> test files at <sha7>)`. A cached
  `still present` stamp of a mechanical claim type is reused only when
  no on-disk check settles the finding differently at the head (such a
  check makes no model call).
- AC15: `pytest -q tests/unit/test_gate_reverifies_at_head.py::test_python_test_written_under_a_nested_tests_dir_resolves_missing_test`
  → green: a `missing_test` on `api/alembic/versions/n0_relabel.py`
  recorded at A where `api/tests/test_relabel.py` does not exist; B adds
  it → `resolved at <B7>: test_relabels_generic_rows exists at
  api/tests/test_relabel.py:4`, merge True, no model call. Before: still
  present, merge False.
- AC16: `pytest -q tests/unit/test_gate_reverifies_at_head.py::test_python_test_absent_at_head_names_what_was_searched`
  → green: the reason ends `(searched 1 test files at <B7>))`.
- AC17: `pytest -q tests/unit/test_gate_reverifies_at_head.py::test_stale_still_present_stamp_at_the_head_yields_to_the_test_found_there`
  → green: a `still present` stamp at B is overridden by the test found
  at B. Pinned for the verifier alone by
  `tests/unit/test_finding_verifiers.py::test_missing_test_refuted_by_a_test_under_a_nested_tests_dir`
  and `::test_missing_test_not_refuted_by_a_test_named_def_in_code_under_review`.

## Amendment (0.2, 2026-10-22): `review pr` re-verifies too (Q1)

Q1 is decided yes. Measured on the demo recording of 2026-10-21
(`repoach-demo` #9, step 6): at the fix head all four reviewers answer
`APPROVE` with no finding, the gate re-verifies three findings of the
planted head and passes, but `repoach ci review` (a `review pr` child)
still reports `REQUEST_CHANGES`, because `summarise_ledger_facts`
counts the judged findings verified at the planted head. NG1 is
withdrawn and replaced by G7-G10; AC10-AC14 pin them.

- G7: `review pr` at head B, reading the pinned head tree
  (SP-REVIEW-PINS-HEAD G2), runs `reverify_older_findings` after the
  refuter pass and before it computes the team verdict. The reviewers
  still review the whole B diff; findings recorded at B are left to the
  refuter pass, exactly as in the gate.
- G8: the verdict counts only the findings open at B: a finding of an
  older head the re-check resolves is `resolved_at_head` and excluded;
  one still present stays counted, and the JSON names it in
  `reverify_reasons` (`finding <id>: <claim> <file>:<line> raised at
  <A7>, still present at <B7> (<why>)`).
- G9: the `review pr` JSON carries the gate's `reverified` block
  (`{"resolved", "still_open", "needs_judgement"}`) and
  `reverify_reasons`; stderr carries one line
  `review: re-verified <n> finding(s) of older heads at <sha7>: resolved <a>, still open <b>, needs judgement <c>`.
  The sticky ledger report carries both: a `Findings of older heads
  re-verified` row in its facts table (when any was re-checked) and the
  carried findings among its decision reasons.
- G10: `review pr` does not read CI: `broken_behavior` findings are not
  re-checked by it (`ci_green=None`) and stay with the gate. The judge
  calls of the re-check come out of the same refuter cap as the refuter
  pass of the review (`cap - refuter calls already made`); a finding the
  budget defers is `needs judgement (judging cap N reached)`, N being the
  configured cap, not the remaining budget; it is not
  stamped, and the next gate judges it. Its outcomes are stamped like
  the gate's, so a second `review pr` or a gate at the same head makes
  no second model call for them. A review that does not read the head
  tree (an orchestrator built without a pinned head) skips the re-check
  with `review: findings of older heads not re-verified (no head tree
  pinned)`, and those findings keep blocking.


## Intent

`repoach review gate` promises a decision "re-verified at the exact
head". For a finding raised at an earlier head it does not keep that
promise: only repoach's own Coder loop moves a finding to `resolved`,
so when a person pushes the fix the gate refuses forever. This spec
makes the gate re-check, at the head it decides on, every open blocking
finding recorded at an older head, settle the ones the head no longer
supports, and name the others.

## Context

Measured on `develop` `f8915fc` (2026-10-21), reproduction on
`repoachhq/repoach-demo` #6 (`docs/tech_debt.md` row 59 of PR #229).

| # | Fact | Where |
|---|---|---|
| F1 | After the fix commit `bbfb62f`, all four reviewers answered `APPROVE` with 0 blocking findings, coverage was `covered`, CI green, and `review gate 6` still refused: `5 open blocking finding(s) at head`, exit 5 | demo recording `demo/recorded/2026-10-21.jsonl`, step 6 |
| F2 | The five were raised at `cf64ff6`: four judged (`design` ×2, `security` ×2) `verified` by the refuter, one `missing_test` on `frontend/src/InvoiceTable.ts:36` `verified` by the `tests/`-only Python symbol search | demo ledger `pr_findings` ids 23, 24, 26, 27, 30 |
| F3 | `gather_merge_facts` counts a judged finding past `proposed` at ANY sha (SP-GATE-JUDGED-FAIL-CLOSED) and re-checks mechanical ones with `verify_finding`, without ever changing a status | `src/repoach/review/merge_gate.py` (`gather_merge_facts`) |
| F4 | `judge_findings_for_pr` judges only `proposed` findings, and with `scope_head` only those of the current head; older ones are counted `stale` and left alone | `src/repoach/review/refuter.py` (`judge_findings_for_pr`) |
| F5 | The only `-> resolved` path is `coder_findings.reverify_resolution_for_pr`, called after the Coder's own push: `verify_finding` for mechanical claims, `refute_finding` for judged ones, `open -> resolved` when the check refutes | `src/repoach/review/coder_findings.py` |
| F6 | The `missing_test` verifier searches `def test_<name>` in `tests/**/*.py`; a Vitest test in `frontend/src/*.test.ts` can never refute it | `src/repoach/review/finding_verifiers.py` (`_verify_missing_test`), `src/repoach/review/hallucination_guard.py` (`make_tests_symbol_searcher`) |
| F7 | `decide_at_head` is the single unit both `review gate` and `review merge` reach, so a change there keeps the two decisions identical | `src/repoach/review/auto_merge.py` (`decide_at_head`) |
| F8 | `compute_spec_coverage` requires `promised_body_non_trivial`, which splits every selector on `::` and parses the file with Python's `ast`; a Vitest selector written with ` > ` is never a file (reported missing with the test present), and the `::` spelling passes only because a TypeScript `SyntaxError` returns `True` | `src/repoach/review/spec_gate.py` (`promised_body_non_trivial`); tech_debt row 60 |

SP-REVIEW-PINS-HEAD G3 (every stamp carries the head) and G4 (no
`APPROVE` over `proposed` blockers) are about recording the head a
finding was raised at. Neither re-checks a finding at a later head:
this spec builds on G2 (the gate's verifiers read the pinned head tree)
and is the missing half.

## Goals

- G1: at gate time, every blocking finding in `proposed`, `verified`,
  `open` or `stuck` whose `head_sha` differs from the decided head is
  re-checked against the head tree before the facts are gathered.
- G2: a finding the head no longer supports moves to the new terminal
  status `resolved_at_head`, with `checked_at_sha` = the head and the
  reason in `verification_result` (`resolved at <sha7>: <why>`).
- G3: a finding the head still supports stays blocking, and the gate's
  reasons name it with the head it was raised at and
  `still present at <sha7>`.
- G4: a finding no mechanical check can settle at the head is handed to
  the refuter at the head (within the refuter cap); one it cannot judge
  either is listed `unresolved: needs judgement (<reason>)` in the
  reasons and keeps blocking — never kept silently.
- G5: the gate JSON carries `reverified: {resolved, still_open,
  needs_judgement}` and stderr one line
  `gate: re-verified <n> finding(s) of older heads at <sha7>: resolved <a>, still open <b>, needs judgement <c>`.
- G6 (tech_debt row 60): `promised_body_non_trivial` dispatches every
  non-pytest selector through `parse_selector`; a Vitest selector (` > `
  or `::`) is judged by its `it`/`test`/`describe` block having a
  non-empty callback body, and a TypeScript file never reaches Python's
  `ast` (a parse error is never a non-empty body).

## Non-Goals

- NG1: withdrawn in 0.2 (see the amendment): `review pr` re-verifies
  older findings too.
- NG2: no change to the Coder loop's `open -> resolved` path.
- NG3: no "the cited lines did not change, so it still holds" shortcut:
  a fix elsewhere in the function would never clear it.
- NG4: `broken_behavior` findings are settled by CI at the head (green
  → resolved), not by a test run of their own.

## Assumptions

- A1: the gate and `review merge` both read the head tree
  (SP-REVIEW-PINS-HEAD G2); the re-check never reads the checkout. A
  `decide_at_head` call that is not told its `repo_root` is the head
  tree skips the re-check with `findings of older heads not
  re-verified (no head tree pinned)`, and those findings keep blocking.
- A2: the refuter cap and the gate configuration come from the
  checkout (trusted), never from the PR head's `repoach.toml`.
- A3: a finding the re-check resolves says nothing about whether it was
  real at its own head; `resolved_at_head` stays out of
  `CONFIRMED_REAL_STATUSES`, so reviewer precision ignores it.

## Interface

```python
class FindingStatus(StrEnum):
    RESOLVED_AT_HEAD = "resolved_at_head"   # terminal, non-blocking

@dataclass(frozen=True)
class Reverification:
    finding_id: int | None
    claim_type: ClaimType
    file: str
    line_start: int
    recorded_at: str
    state: Literal["resolved", "still_open", "needs_judgement"]
    reason: str

@dataclass(frozen=True)
class HeadReverification:
    head_sha: str
    outcomes: tuple[Reverification, ...] = ()
    model_calls: int = 0
    def counts(self) -> dict[str, int]: ...
    def summary_line(self) -> str: ...
    def reasons(self) -> list[str]: ...

def reverify_older_findings(
    db_path: Path, *, pr_number: int, repo_root: Path, head_sha: str,
    ci_green: bool, config: GatesConfig, max_judged: int,
    judge_factory: Callable[[], Judge] = make_refuter_judge,
) -> HeadReverification: ...
```

`decide_at_head` runs `reverify_older_findings` before
`gather_merge_facts` and returns facts carrying `reverified` (the
counts) and `reverify_reasons`; `compute_merge_decision` appends those
reasons when it refuses.

Re-check per claim type:

| Claim | Check at head | resolved when | still open when | otherwise |
|---|---|---|---|---|
| `missing_test` / `missing_docstring` / `lint_convention` | `verify_finding` | it refutes, or the cited file is gone | it verifies | refuter |
| `missing_test` on a JavaScript/TypeScript file | the symbol declared on the cited line at the finding's head, looked for in the code of every running `it`/`test` block of the Vitest test files (comments, strings, titles, `.skip`/`.todo` blocks ignored) | such a block uses it, in a test file new or changed since the finding's head | none does | refuter (no symbol on the line; any other non-Python language) |
| `design` / `security` / `spec_gap` | refuter, cited evidence read at head | refuted, or the cited file is gone | verified | needs judgement |
| `broken_behavior` | CI at head | green | red | — |

A finding already re-checked at this head (`checked_at_sha` = head,
recorded at another head) keeps its stored outcome — `still_open`, or
`needs_judgement` unless the cap deferred it — without a second model
call; `broken_behavior` is always re-read from CI, which may have
turned green since. Findings carrying the same claim (claim type,
file, lines and normalised text — several reviewers raising one
defect) are re-checked once and cost one judge call.

Outputs:
- gate JSON: `reverified: {"resolved": int, "still_open": int, "needs_judgement": int}`.
- stderr: the G5 line, once per gate that reached the ledger.

Errors: none new. A judge that cannot be built or raises leaves every
judged finding `needs judgement (judge call failed: <type>)`.

## Behavior

### Nominal
Head A plants a SQL injection; the review verifies a `security`
finding at A. A person pushes B binding the parameter. `review gate`
at B: the refuter reads `store.py` at B, refutes, the finding becomes
`resolved_at_head` with `resolved at <B7>: <reasoning>`; with CI green,
a complete review at B and coverage held, the gate exits 0 and prints
`gate: re-verified 1 finding(s) of older heads at <B7>: resolved 1, still open 0, needs judgement 0`.

### Edge cases
- the fix does not remove the defect → the refuter verifies, the
  finding stays `verified` with `checked_at_sha` = B, the reasons say
  `finding <id>: security src/x.py:3 raised at <A7>, still present at <B7>`, exit 5.
- the gate runs twice at B → the second run makes no model call.
- `review merge` with the checkout on the base branch (the cited file
  absent there) → the head tree is still the one read; the finding is
  never resolved by the checkout's absence.
- a first gate with CI pending, a second with CI green, same head → a
  `broken_behavior` finding resolves on the second.
- more judged findings than the cap → the rest are `needs judgement
  (judging cap N reached)`.
- a finding recorded at B itself → untouched (the review at B owns it).

### Failure scenarios
- no provider / the judge raises → every judged finding is `needs
  judgement (judge call failed: <type>)` and blocks; exit 5.
- `head_sha` empty → nothing is re-checked; the gate already refuses
  `head_sha unknown`.

## Architecture Impact
- Adds dependency: SP-GATE-REVERIFIES-AT-HEAD -> SP-FINDINGS-INIT-RACE (`head_reverify.py` reads and transitions `pr_findings` rows; `findings.py` gains `RESOLVED_AT_HEAD`)
- Adds dependency: SP-GATE-REVERIFIES-AT-HEAD -> SP-REFUTER-JUDGES-WHAT-IT-CAN (`verify_finding`, `refute_finding`, `make_refuter_judge`)
- Adds dependency: SP-GATE-REVERIFIES-AT-HEAD -> SP-UNIVERSAL-GATES (`GatesConfig`, `is_js_test_file`, `parse_selector` for the Vitest body check)
- Adds dependency: SP-GATE-REVERIFIES-AT-HEAD -> SP-REVIEW-PINS-HEAD (the gate tests materialise the head tree through `review_head`)
- Adds dependency: SP-GATE-REVERIFIES-AT-HEAD -> SP-DEDUP-CLASSIFICATION-CONSTANTS (owner of `merge_gate.py`: `MergeFacts` gains `reverified`, `reverify_reasons`, `reverify_line`; the tests build one)
- Adds dependency: SP-GATE-REVERIFIES-AT-HEAD -> SP-PLAN-SELECTOR-AUDIT-WIRE (owner of `spec_gate.py`: G6 changes `promised_body_non_trivial`; the tests call it)
- Adds dependency: SP-GATE-REVERIFIES-AT-HEAD -> SP-PLAN-CONTRACT-LINTS (the coverage test builds an `ActionPlan`)
- Adds dependency: SP-GATE-REVERIFIES-AT-HEAD -> SP-UNIVERSAL-FORGE (the gate tests drive a `GitHubForge` with a real `git`)
- Adds dependency: SP-GATE-REVERIFIES-AT-HEAD -> SP-ORCH-DOCSTRING (0.2: owner of `orchestrator.py`; the `review pr` tests drive `run_review` and read `TeamOutcome`)
- Adds dependency: SP-GATE-REVERIFIES-AT-HEAD -> SP-RETRY-BACKOFF-DEDUP (0.2: owner of `reviewer.py`; the `review pr` tests build `ReviewerOutcome` stubs)
- `orchestrator.py` (0.2) gains the call into `head_reverify.py`; `head_reverify` imports nothing from the orchestrator.
- `auto_merge.py` is frontier; it gains the call into `head_reverify.py`. No cycle: `head_reverify` imports neither `merge_gate` nor `auto_merge`.

## Diagram
```mermaid
flowchart TD
    A[gate at head B] --> B[blocking findings raised at older heads]
    B --> C{mechanical check at B}
    C -->|refutes| R[(resolved_at_head B7)]
    C -->|verifies| O[still present at B7]
    C -->|cannot judge| J{refuter at B, within cap}
    J -->|refuted| R
    J -->|verified| O
    J -->|no verdict| N[needs judgement, blocks]
```

## Acceptance Criteria
- [ ] AC1: `pytest -q tests/unit/test_gate_reverifies_at_head.py::test_human_fix_resolves_the_finding_and_the_gate_passes` → green: a git repository whose head A plants an f-string SQL query, a `security` finding verified at A, a fix commit B; `evaluate_merge_gate` at B with a refuter that refutes over the head evidence → the finding is `resolved_at_head`, `verification_result` starts `resolved at <B7>`, `decision.merge is True`. Today: `5 open blocking finding(s)`-shaped refusal, merge False.
- [ ] AC2: `pytest -q tests/unit/test_gate_reverifies_at_head.py::test_fix_that_keeps_the_defect_is_named_still_present` → green: B keeps the f-string; the reasons contain `raised at <A7>, still present at <B7>`, merge False.
- [ ] AC3: `pytest -q tests/unit/test_gate_reverifies_at_head.py::test_unjudgeable_finding_is_needs_judgement_not_silent` → green: the judge factory raises; the reasons contain `unresolved: needs judgement (judge call failed: RuntimeError)`, `reverified == {"resolved": 0, "still_open": 0, "needs_judgement": 1}`, merge False.
- [ ] AC4: `pytest -q tests/unit/test_gate_reverifies_at_head.py::test_vitest_test_added_at_head_resolves_missing_test` → green: a `missing_test` on `frontend/src/t.ts:1` (`export function dueTotal`) verified at A; B adds `frontend/src/t.test.ts` naming `dueTotal` → `resolved_at_head`, no model call.
- [ ] AC5: `pytest -q tests/unit/test_gate_reverifies_at_head.py::test_second_gate_at_the_same_head_makes_no_model_call` → green.
- [ ] AC6: `repoach review gate <N> 2>&1 >/dev/null | grep '^gate: re-verified'` → one line; `repoach review gate <N> 2>/dev/null | jq -c .reverified` → `{"resolved":…,"still_open":…,"needs_judgement":…}`. Pinned by `pytest -q tests/unit/test_gate_reverifies_at_head.py::test_cli_prints_the_line_and_the_json_key`.
- [ ] AC7: `pytest -q tests/unit/test_spec_gate_vitest_body.py` → green: a Vitest selector with ` > ` and with `::` whose `it` block has a body is present; an `it` with an empty callback, an `it.todo`, and a `.ts` file that is not valid Python are not non-trivial bodies. Today: ` > ` reported missing, `::` on an empty `it` reported present.
- [ ] AC8: `cd repoach-demo && make demo DEMO_MODE=live` with `REPOACH_PROVIDER=claude_code` → last line `demo: 7/7 steps, rcs 0 0 0 5 0 0 0`.
- [ ] AC9: `repoach arch check --base origin/develop` and `--base origin/main` → `edge-honesty: ok`.
- [ ] AC10: `pytest -q tests/unit/test_review_pr_reverifies_at_head.py::test_review_at_the_fix_head_approves` → green: a `security` finding verified at A, a fix head B, reviewers with no finding at B, a refuter that refutes over B's evidence; `run_review` at B → the finding is `resolved_at_head`, `final_verdict` is `APPROVE`, `reverified == {"resolved": 1, "still_open": 0, "needs_judgement": 0}`. Today: `REQUEST_CHANGES`.
- [ ] AC11: `pytest -q tests/unit/test_review_pr_reverifies_at_head.py::test_review_at_a_non_fix_head_names_the_carried_finding` → green: B keeps the defect; `final_verdict` is `REQUEST_CHANGES` and `reverify_reasons` contains `raised at <A7>, still present at <B7>`.
- [ ] AC12: `pytest -q tests/unit/test_review_pr_reverifies_at_head.py::test_second_review_and_gate_at_the_same_head_make_no_model_call` → green: a second `review pr` at B, then `review gate` at B, make no judge call for the carried finding and each reports it once.
- [ ] AC13: `pytest -q tests/unit/test_review_pr_reverifies_at_head.py::test_cli_prints_the_line_and_the_json_keys` → green: `repoach review pr <N> --dry-run` prints one `review: re-verified` line on stderr and `reverified` / `reverify_reasons` in its JSON.
- [ ] AC14: `cd repoach-demo && make demo DEMO_MODE=live` with `REPOACH_PROVIDER=claude_code` → step 6's `repoach ci review` reports `final_verdict` `APPROVE`, and the last line is still `demo: 7/7 steps, rcs 0 0 0 5 0 0 0`.

## Open Questions
- Q1: decided yes in 0.2 (see the amendment, G7-G10).
