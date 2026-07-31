---
id: SP-SELFVERIFY-REFUTABLE-GAPS
title: Self-verify judge gaps must be mechanically refutable
version: 0.1
status: approved
author: jfaye (SP-REGEN-FRESH-CELLS judge false positives, 2026-07-22)
created: 2026-07-22
updated: 2026-07-22

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Self-verify judge gaps must be mechanically refutable

## Intent

The self-verify judge blocked a fully green delivery on claims the
tree disproves. Live occurrences (2026-07-21/22, SP-REGEN-FRESH-CELLS
session, diff 68.9k chars — under the 100k cap, so the judge SAW the
hunks it declared missing):

- "gather_and_regenerate calls the sweep with no try/except and emits
  no chain_regen_sweep_failed log" — the committed src carries exactly
  that except-and-log, and the test the judge said "would fail" passes;
- "the diff only bumps SP-MFC-REGEN's version without adding
  SP-CREDITS-CHECK to depends_on" — the committed frontmatter contains
  the entry, twice re-asserted across reruns.

Additionally the verdicts are unstable across identical-evidence
reruns (4 gaps → 0 on one resume; 1 gap → 2 different gaps on
another). An LLM judge remains the right tool for SEMANTIC
spec-vs-diff compliance, but a claim of the form "X is absent" is a
MECHANICAL fact — the runner can and must check it before honoring
the verdict. A judge that must show checkable evidence loses the
power to hallucinate a blocker; the loud-refusal authority stays
intact for everything it can actually see.

## Context

- `src/repoach/review/devagent_selfverify.py` — one-shot judge over
  `AgentLoop` (OPUS tier, `judge_selfverify_0.1.1.md` persona);
  `_parse_judge_verdict` extracts `{compliant, reasons, gaps[]}` where
  gaps are free strings; `run_selfverify` (the public entry the
  dev_runner gates the push on) honors `compliant` blindly.
- `prompts/review/judge_selfverify_0.1.1.md` — the persona; version
  pinned by `_PERSONA` in the module. Any contract change bumps it.
- The refutation idea mirrors the review bench's Refuter
  (SP-REFUTER-INJECTION-HARDEN): adversarially test a finding before
  it costs anyone anything.

## Goals

- G1 (evidence contract): the persona (bumped to 0.2.0) instructs the
  judge to attach, to every gap asserting an ABSENCE ("not
  implemented", "no test does X", "frontmatter lacks Y"), an evidence
  object: `{"claim": str, "file": "<repo-relative path>",
  "absent_pattern": "<python regex>"}` meaning "this pattern does not
  occur in this file". The verdict JSON shape becomes
  `{compliant, reasons, gaps: [{claim, file?, absent_pattern?}]}`;
  plain-string gaps stay accepted for pure-semantic claims.
- G2 (mechanical refutation): after parsing, the runner checks every
  gap that carries evidence: if `file` exists at HEAD and
  `absent_pattern` MATCHES its content, the gap is REFUTED — dropped
  from the verdict with one `selfverify.gap_refuted` log per drop
  (claim, file, pattern).
- G3 (overturn): when every gap of a non-compliant verdict is refuted,
  the verdict becomes compliant; a single
  `selfverify.verdict_overturned_by_refutation` log records the full
  original verdict for audit.
- G4 (fail-closed on unverifiable evidence): a gap whose evidence is
  malformed (regex that does not compile, path outside the repo or
  absent file) is KEPT as blocking, with a
  `selfverify.gap_evidence_invalid` log — bad evidence never launders
  a gap away.
- G5 (bounded): at most 10 gaps are evidence-checked; beyond that the
  verdict is honored as-is (a judge emitting 11+ gaps has bigger
  problems than refutation).

## Non-Goals

- NG1: no change to the judge's authority over PRESENCE/semantic
  claims ("the test exists but asserts the wrong thing" is not
  refutable by grep and stays blocking).
- NG2: no multi-judge quorum, no diff chunking, no cap change — this
  spec makes single verdicts accountable, not redundant.
- NG3: no change to the mechanical half of self-verify.

## Assumptions

- A1: `re` with `re.M` over the file text is expressive enough for
  the absence claims observed (symbol presence, log-event literals,
  frontmatter entries).
- A2: the AgentLoop boundary is fake-able in tests exactly as
  `run_selfverify`'s existing tests fake it (truthful boundary fake —
  the LLM reply is external input).

## Interface

- `JudgeVerdict` gains structured gaps: a list of
  `JudgeGap {claim: str, file: str | None, absent_pattern: str | None}`
  (a bare string parses into claim-only). `run_selfverify`'s signature
  and return type are unchanged.
- `_PERSONA` pin → `judge_selfverify_0.2.0.md`.

## Behavior

### Nominal

Judge claims "no chain_regen_sweep_failed log in chain_regen.py" with
evidence `{file: src/repoach/llm_proxy/routing/chain_regen.py,
absent_pattern: chain_regen_sweep_failed}`; the pattern matches at
HEAD → gap refuted and dropped; it was the only gap → verdict
overturned, push proceeds, both events logged.

### Edge cases

- Mixed verdict: one refuted gap + one semantic gap → still
  non-compliant; only the refuted one is dropped.
- Pattern matches in a DIFFERENT file than cited → not refuted (the
  claim was about that file).
- Evidence on a compliant verdict: ignored (nothing to refute).
- Regex metacharacters in a literal claim: the persona instructs
  `re.escape`-style literals when in doubt; a non-compiling pattern
  falls under G4.

### Failure scenarios

- File read fails (encoding, size) → treat as G4 invalid evidence,
  gap kept, logged.
- The refutation pass itself raising must never crash self-verify:
  any unexpected exception keeps the ORIGINAL verdict and logs
  `selfverify.refutation_failed` (fail-closed to the judge's word).

## Architecture Impact

- Adds/Removes dependency: none — stdlib `re` inside the existing
  module; persona bump follows the pinned-filename convention.
- New / changed coupling, cycles, or shared state: none.

## Acceptance Criteria

- [ ] AC1: unit — new file `tests/unit/test_selfverify_refutation.py`
  driving the refutation pass with real files under `tmp_path` and a
  truthful boundary-fake AgentLoop reply (no monkeypatching of
  repoach functions): `test_refuted_gap_is_dropped_and_logged`,
  `test_all_gaps_refuted_overturns_verdict` (asserts BOTH events and
  `ok=True`), `test_semantic_gap_survives_refutation` (mixed verdict
  stays failed), `test_invalid_regex_keeps_gap` and
  `test_missing_file_keeps_gap` (G4 fail-closed).
- [ ] AC2: unit — `test_plain_string_gaps_still_parse` (backward
  shape) and `test_gap_cap_beyond_ten_honored_as_is` (G5) in the same
  file.
- [ ] AC3: persona `judge_selfverify_0.2.0.md` exists with the
  evidence contract and an explicit example gap object; `_PERSONA`
  pin updated; the 0.1.1 file is deleted (git history keeps it).
- [ ] AC4 (INTEGRATION): new file
  `tests/integration/test_selfverify_refutation_flow.py::test_false_absence_verdict_overturned_end_to_end`
  — a tmp repo whose file CONTAINS the pattern, a boundary-fake judge
  reply claiming its absence with evidence, `run_selfverify` returns
  `ok=True` and the audit events are captured.
- [ ] AC5: `ruff` + format green; zero inline comments; no `# noqa`;
  full `pytest tests/unit` green; net new non-test code ≤ 150 lines.

## Open Questions

None.
