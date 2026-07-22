# SP-SELFVERIFY-REFUTABLE-GAPS — Self-verify judge gaps must be mechanically refutable

Bump the self-verify judge persona to 0.2.0 with an evidence contract on absence-claims, add a bounded mechanical refutation pass in devagent_selfverify.py that drops refuted gaps (with audit logs), overturns verdicts when every gap is refuted, and fail-closes on malformed evidence; cover with unit and integration tests.

## Step 1 — Bump persona to 0.2.0 with evidence contract; retire 0.1.1

- **Files**: `prompts/review/judge_selfverify_0.2.0.md`, `prompts/review/judge_selfverify_0.1.1.md`, `tests/unit/test_selfverify_persona.py`
- **Action**: Create prompts/review/judge_selfverify_0.2.0.md mirroring the 0.1.1 structure but updating the Output contract section: gaps become a list of objects {claim: str, file?: str, absent_pattern?: str} for absence claims (plain strings still accepted for pure-semantic claims), with an explicit example gap object literal and a note that absent_pattern is a Python regex (re.M) and should be re.escape'd when in doubt. Delete prompts/review/judge_selfverify_0.1.1.md (git history keeps it). Create tests/unit/test_selfverify_persona.py with one test that reads the 0.2.0 file and asserts it contains 'absent_pattern' and an example gap object literal.
- **Commit**: `chore(prompts): bump selfverify judge persona to 0.2.0 with evidence contract`
- **Done when**: pytest tests/unit/test_selfverify_persona.py -q exits 0 && ! test -f prompts/review/judge_selfverify_0.1.1.md
- **Unit tests**: `tests/unit/test_selfverify_persona.py::test_persona_0_2_0_has_evidence_contract`

## Step 2 — Structured gaps, refutation pass, overturn, fail-closed logging

- **Files**: `src/repoach/review/devagent_selfverify.py`, `tests/unit/test_selfverify_refutation.py`
- **Action**: In src/repoach/review/devagent_selfverify.py: (a) add a JudgeGap dataclass {claim: str, file: str | None = None, absent_pattern: str | None = None}; (b) change JudgeVerdict.gaps to list[JudgeGap] and update _parse_judge_verdict so each list element parses as either a bare string (claim-only) or a dict with claim/file/absent_pattern (missing keys default to None); (c) bump _PERSONA to 'judge_selfverify_0.2.0.md'; (d) add a private _refute_gaps(verdict, repo_root) -> JudgeVerdict helper that, for at most 10 evidence-bearing gaps, attempts to refute each: if file exists at repo_root and re.compile(absent_pattern, re.M) matches its text, the gap is dropped and selfverify.gap_refuted is logged with claim/file/pattern; (e) when the original verdict was non-compliant and every gap was refuted, set compliant=True and log selfverify.verdict_overturned_by_refutation with the full original verdict; (f) wire _refute_gaps into run_self_verify after _judge_compliance, before the ok computation, only when verdict.available and not verdict.compliant; (g) keep run_self_verify's signature and SelfVerifyResult shape unchanged. Create tests/unit/test_selfverify_refutation.py with three tests: test_refuted_gap_is_dropped_and_logged (file contains pattern, gap with evidence is dropped, selfverify.gap_refuted logged), test_all_gaps_refuted_overturns_verdict (single refuted gap, verdict becomes compliant, selfverify.verdict_overturned_by_refutation logged, ok=True), test_semantic_gap_survives_refutation (mixed verdict: one refuted gap + one plain-string semantic gap, stays non-compliant, only refuted one dropped).
- **Commit**: `feat(review): add mechanical refutation pass for selfverify judge gaps`
- **Done when**: pytest tests/unit/test_selfverify_refutation.py::test_refuted_gap_is_dropped_and_logged tests/unit/test_selfverify_refutation.py::test_all_gaps_refuted_overturns_verdict tests/unit/test_selfverify_refutation.py::test_semantic_gap_survives_refutation exits 0
- **Unit tests**: `tests/unit/test_selfverify_refutation.py::test_refuted_gap_is_dropped_and_logged`, `tests/unit/test_selfverify_refutation.py::test_all_gaps_refuted_overturns_verdict`, `tests/unit/test_selfverify_refutation.py::test_semantic_gap_survives_refutation`

## Step 3 — Edge cases, gap cap, and end-to-end integration test

- **Files**: `src/repoach/review/devagent_selfverify.py`, `tests/unit/test_selfverify_refutation.py`, `tests/integration/test_selfverify_refutation_flow.py`
- **Action**: Extend tests/unit/test_selfverify_refutation.py with four additional tests covering edge cases: test_invalid_regex_keeps_gap (gap with malformed regex pattern, gap is KEPT, selfverify.gap_evidence_invalid logged), test_missing_file_keeps_gap (gap with file path that does not exist at repo_root, gap is KEPT, selfverify.gap_evidence_invalid logged), test_plain_string_gaps_still_parse (verdict with bare-string gaps parses correctly into JudgeGap with file=None and absent_pattern=None, backward shape preserved), test_gap_cap_beyond_ten_honored_as_is (verdict with 11 evidence-bearing gaps, refutation pass is skipped entirely, original verdict honored as-is). If step 2 did not already implement the G4 fail-closed logging or the G5 cap, add minimal handling in src/repoach/review/devagent_selfverify.py to make these tests pass. Create tests/integration/test_selfverify_refutation_flow.py with test_false_absence_verdict_overturned_end_to_end: a tmp repo whose file CONTAINS the pattern, a boundary-fake judge reply claiming its absence with evidence, run_self_verify returns ok=True and the audit events are captured.
- **Commit**: `test(review): cover selfverify refutation edge cases and end-to-end overturn`
- **Done when**: pytest tests/unit/test_selfverify_refutation.py::test_invalid_regex_keeps_gap tests/unit/test_selfverify_refutation.py::test_missing_file_keeps_gap tests/unit/test_selfverify_refutation.py::test_plain_string_gaps_still_parse tests/unit/test_selfverify_refutation.py::test_gap_cap_beyond_ten_honored_as_is tests/integration/test_selfverify_refutation_flow.py::test_false_absence_verdict_overturned_end_to_end exits 0
- **Unit tests**: `tests/unit/test_selfverify_refutation.py::test_invalid_regex_keeps_gap`, `tests/unit/test_selfverify_refutation.py::test_missing_file_keeps_gap`, `tests/unit/test_selfverify_refutation.py::test_plain_string_gaps_still_parse`, `tests/unit/test_selfverify_refutation.py::test_gap_cap_beyond_ten_honored_as_is`

## Integration tests

- `tests/integration/test_selfverify_refutation_flow.py::test_false_absence_verdict_overturned_end_to_end`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-SELFVERIFY-REFUTABLE-GAPS",
  "title": "Self-verify judge gaps must be mechanically refutable",
  "summary": "Bump the self-verify judge persona to 0.2.0 with an evidence contract on absence-claims, add a bounded mechanical refutation pass in devagent_selfverify.py that drops refuted gaps (with audit logs), overturns verdicts when every gap is refuted, and fail-closes on malformed evidence; cover with unit and integration tests.",
  "steps": [
    {
      "index": 1,
      "title": "Bump persona to 0.2.0 with evidence contract; retire 0.1.1",
      "files": [
        "prompts/review/judge_selfverify_0.2.0.md",
        "prompts/review/judge_selfverify_0.1.1.md",
        "tests/unit/test_selfverify_persona.py"
      ],
      "action": "Create prompts/review/judge_selfverify_0.2.0.md mirroring the 0.1.1 structure but updating the Output contract section: gaps become a list of objects {claim: str, file?: str, absent_pattern?: str} for absence claims (plain strings still accepted for pure-semantic claims), with an explicit example gap object literal and a note that absent_pattern is a Python regex (re.M) and should be re.escape'd when in doubt. Delete prompts/review/judge_selfverify_0.1.1.md (git history keeps it). Create tests/unit/test_selfverify_persona.py with one test that reads the 0.2.0 file and asserts it contains 'absent_pattern' and an example gap object literal.",
      "commit_message": "chore(prompts): bump selfverify judge persona to 0.2.0 with evidence contract",
      "done_when": "pytest tests/unit/test_selfverify_persona.py -q exits 0 && ! test -f prompts/review/judge_selfverify_0.1.1.md",
      "unit_tests": [
        "tests/unit/test_selfverify_persona.py::test_persona_0_2_0_has_evidence_contract"
      ]
    },
    {
      "index": 2,
      "title": "Structured gaps, refutation pass, overturn, fail-closed logging",
      "files": [
        "src/repoach/review/devagent_selfverify.py",
        "tests/unit/test_selfverify_refutation.py"
      ],
      "action": "In src/repoach/review/devagent_selfverify.py: (a) add a JudgeGap dataclass {claim: str, file: str | None = None, absent_pattern: str | None = None}; (b) change JudgeVerdict.gaps to list[JudgeGap] and update _parse_judge_verdict so each list element parses as either a bare string (claim-only) or a dict with claim/file/absent_pattern (missing keys default to None); (c) bump _PERSONA to 'judge_selfverify_0.2.0.md'; (d) add a private _refute_gaps(verdict, repo_root) -> JudgeVerdict helper that, for at most 10 evidence-bearing gaps, attempts to refute each: if file exists at repo_root and re.compile(absent_pattern, re.M) matches its text, the gap is dropped and selfverify.gap_refuted is logged with claim/file/pattern; (e) when the original verdict was non-compliant and every gap was refuted, set compliant=True and log selfverify.verdict_overturned_by_refutation with the full original verdict; (f) wire _refute_gaps into run_self_verify after _judge_compliance, before the ok computation, only when verdict.available and not verdict.compliant; (g) keep run_self_verify's signature and SelfVerifyResult shape unchanged. Create tests/unit/test_selfverify_refutation.py with three tests: test_refuted_gap_is_dropped_and_logged (file contains pattern, gap with evidence is dropped, selfverify.gap_refuted logged), test_all_gaps_refuted_overturns_verdict (single refuted gap, verdict becomes compliant, selfverify.verdict_overturned_by_refutation logged, ok=True), test_semantic_gap_survives_refutation (mixed verdict: one refuted gap + one plain-string semantic gap, stays non-compliant, only refuted one dropped).",
      "commit_message": "feat(review): add mechanical refutation pass for selfverify judge gaps",
      "done_when": "pytest tests/unit/test_selfverify_refutation.py::test_refuted_gap_is_dropped_and_logged tests/unit/test_selfverify_refutation.py::test_all_gaps_refuted_overturns_verdict tests/unit/test_selfverify_refutation.py::test_semantic_gap_survives_refutation exits 0",
      "unit_tests": [
        "tests/unit/test_selfverify_refutation.py::test_refuted_gap_is_dropped_and_logged",
        "tests/unit/test_selfverify_refutation.py::test_all_gaps_refuted_overturns_verdict",
        "tests/unit/test_selfverify_refutation.py::test_semantic_gap_survives_refutation"
      ]
    },
    {
      "index": 3,
      "title": "Edge cases, gap cap, and end-to-end integration test",
      "files": [
        "src/repoach/review/devagent_selfverify.py",
        "tests/unit/test_selfverify_refutation.py",
        "tests/integration/test_selfverify_refutation_flow.py"
      ],
      "action": "Extend tests/unit/test_selfverify_refutation.py with four additional tests covering edge cases: test_invalid_regex_keeps_gap (gap with malformed regex pattern, gap is KEPT, selfverify.gap_evidence_invalid logged), test_missing_file_keeps_gap (gap with file path that does not exist at repo_root, gap is KEPT, selfverify.gap_evidence_invalid logged), test_plain_string_gaps_still_parse (verdict with bare-string gaps parses correctly into JudgeGap with file=None and absent_pattern=None, backward shape preserved), test_gap_cap_beyond_ten_honored_as_is (verdict with 11 evidence-bearing gaps, refutation pass is skipped entirely, original verdict honored as-is). If step 2 did not already implement the G4 fail-closed logging or the G5 cap, add minimal handling in src/repoach/review/devagent_selfverify.py to make these tests pass. Create tests/integration/test_selfverify_refutation_flow.py with test_false_absence_verdict_overturned_end_to_end: a tmp repo whose file CONTAINS the pattern, a boundary-fake judge reply claiming its absence with evidence, run_self_verify returns ok=True and the audit events are captured.",
      "commit_message": "test(review): cover selfverify refutation edge cases and end-to-end overturn",
      "done_when": "pytest tests/unit/test_selfverify_refutation.py::test_invalid_regex_keeps_gap tests/unit/test_selfverify_refutation.py::test_missing_file_keeps_gap tests/unit/test_selfverify_refutation.py::test_plain_string_gaps_still_parse tests/unit/test_selfverify_refutation.py::test_gap_cap_beyond_ten_honored_as_is tests/integration/test_selfverify_refutation_flow.py::test_false_absence_verdict_overturned_end_to_end exits 0",
      "unit_tests": [
        "tests/unit/test_selfverify_refutation.py::test_invalid_regex_keeps_gap",
        "tests/unit/test_selfverify_refutation.py::test_missing_file_keeps_gap",
        "tests/unit/test_selfverify_refutation.py::test_plain_string_gaps_still_parse",
        "tests/unit/test_selfverify_refutation.py::test_gap_cap_beyond_ten_honored_as_is"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_selfverify_refutation_flow.py::test_false_absence_verdict_overturned_end_to_end"
  ]
}
```
