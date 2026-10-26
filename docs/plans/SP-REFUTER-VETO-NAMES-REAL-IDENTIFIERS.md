# SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS — Common words and fully served identifiers never defer a refuter verdict

Drop Python names, common tokens and generic English words from a claim's identifiers, let only cuts that matter to the cited file veto a refutation, name the rule in the deferral reason, and key the needs-judgement stamp on the rule version and context.

## Step 1 — Drop Python names, common tokens and English words from a claim's identifiers

- **Files**: `src/repoach/review/refuter_evidence.py`, `tests/unit/test_refuter_veto_names_real_identifiers.py`
- **Action**: Add STOP_WORDS, is_stop_word and REFUTER_RULE_VERSION to refuter_evidence and skip stop words in claim_identifiers, keeping the 4-character floor.
- **Commit**: `fix(refuter): never search common words as claim identifiers`
- **Done when**: pytest tests/unit/test_refuter_veto_names_real_identifiers.py -k 'stop_list or measured or shorter' passes
- **Unit tests**: `tests/unit/test_refuter_veto_names_real_identifiers.py::test_stop_list_holds_python_names_common_tokens_and_english_words`, `tests/unit/test_refuter_veto_names_real_identifiers.py::test_measured_common_words_are_never_identifiers`, `tests/unit/test_refuter_veto_names_real_identifiers.py::test_measured_claims_keep_their_real_identifiers`, `tests/unit/test_refuter_veto_names_real_identifiers.py::test_identifiers_shorter_than_four_characters_are_dropped`, `tests/unit/test_refuter_veto_names_real_identifiers.py::test_measured_words_never_veto_a_refutation`

## Step 2 — An identifier fully served in the cited file never vetoes

- **Files**: `src/repoach/review/refuter_evidence.py`, `tests/unit/test_refuter_veto_names_real_identifiers.py`
- **Action**: Classify each searched identifier as unserved, partial (a hit in the cited file cut, or the cited file holds none and a hit elsewhere cut) or partial_elsewhere, and expose partial_in_cited and partial_elsewhere on EvidenceBundle.
- **Commit**: `fix(refuter): judge a partial identifier by the cited file`
- **Done when**: pytest tests/unit/test_refuter_veto_names_real_identifiers.py -k 'cited_file or classified' passes
- **Unit tests**: `tests/unit/test_refuter_veto_names_real_identifiers.py::test_identifier_fully_served_in_the_cited_file_does_not_veto`, `tests/unit/test_refuter_veto_names_real_identifiers.py::test_identifier_absent_from_the_cited_file_and_cut_elsewhere_still_vetoes`, `tests/unit/test_refuter_veto_names_real_identifiers.py::test_cut_inside_the_cited_file_is_classified_in_cited`

## Step 3 — The deferral reason names the rule that held

- **Files**: `src/repoach/review/refuter.py`, `tests/unit/test_refuter_defers_unseen.py`, `tests/integration/test_refuter_defers_unserved_end_to_end.py`
- **Action**: Veto a refutation only on EvidenceBundle.partial, with EVIDENCE_PARTLY_SERVED_IN_CITED or EVIDENCE_PARTLY_SERVED_OUTSIDE, and reword EVIDENCE_NOT_SERVED to 'identifier not served: <ids>'.
- **Commit**: `fix(refuter): name the rule that defers a verdict`
- **Done when**: pytest tests/unit/test_refuter_defers_unseen.py tests/unit/test_refuter_veto_names_real_identifiers.py passes
- **Unit tests**: `tests/unit/test_refuter_veto_names_real_identifiers.py::test_deferral_reason_names_the_rule_that_held`, `tests/unit/test_refuter_defers_unseen.py::test_refutation_resting_on_a_partly_served_identifier_is_deferred`, `tests/unit/test_refuter_defers_unseen.py::test_hits_cut_by_the_budget_defer_without_calling_the_judge`, `tests/unit/test_refuter_defers_unseen.py::test_unserved_reason_is_a_closed_set_template`

## Step 4 — Re-judge a needs-judgement stamp when the rules or the context change

- **Files**: `src/repoach/review/head_reverify.py`, `tests/unit/test_refuter_veto_names_real_identifiers.py`, `tests/integration/test_refuter_veto_replays_pr260.py`
- **Action**: Stamp a needs_judgement outcome with judgement_key(context_tokens) and reuse it at the same head only under the same key; add the context_tokens parameter to reverify_older_findings; replay PR #260 findings 41 and 66 end to end through reverify_older_findings.
- **Commit**: `fix(reverify): re-judge needs judgement when the rules change`
- **Done when**: pytest tests/unit/test_refuter_veto_names_real_identifiers.py -k 'needs_judgement or stamp' passes
- **Unit tests**: `tests/unit/test_refuter_veto_names_real_identifiers.py::test_needs_judgement_is_reused_only_under_the_same_key`, `tests/unit/test_refuter_veto_names_real_identifiers.py::test_needs_judgement_is_re_judged_at_a_new_head`, `tests/unit/test_refuter_veto_names_real_identifiers.py::test_needs_judgement_is_re_judged_when_the_context_changes`, `tests/unit/test_refuter_veto_names_real_identifiers.py::test_needs_judgement_is_re_judged_on_a_rule_version_bump`, `tests/unit/test_refuter_veto_names_real_identifiers.py::test_stamp_written_before_the_rule_version_is_re_judged`

## Integration tests

- `tests/integration/test_refuter_veto_replays_pr260.py::test_measured_refutation_settles_the_finding_at_the_next_head`
- `tests/integration/test_refuter_defers_unserved_end_to_end.py::test_unserved_identifier_is_deferred_through_the_review_without_a_judge_call`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS",
  "title": "Common words and fully served identifiers never defer a refuter verdict",
  "summary": "Drop Python names, common tokens and generic English words from a claim's identifiers, let only cuts that matter to the cited file veto a refutation, name the rule in the deferral reason, and key the needs-judgement stamp on the rule version and context.",
  "steps": [
    {
      "index": 1,
      "title": "Drop Python names, common tokens and English words from a claim's identifiers",
      "files": [
        "src/repoach/review/refuter_evidence.py",
        "tests/unit/test_refuter_veto_names_real_identifiers.py"
      ],
      "action": "Add STOP_WORDS, is_stop_word and REFUTER_RULE_VERSION to refuter_evidence and skip stop words in claim_identifiers, keeping the 4-character floor.",
      "commit_message": "fix(refuter): never search common words as claim identifiers",
      "done_when": "pytest tests/unit/test_refuter_veto_names_real_identifiers.py -k 'stop_list or measured or shorter' passes",
      "unit_tests": [
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_stop_list_holds_python_names_common_tokens_and_english_words",
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_measured_common_words_are_never_identifiers",
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_measured_claims_keep_their_real_identifiers",
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_identifiers_shorter_than_four_characters_are_dropped",
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_measured_words_never_veto_a_refutation"
      ]
    },
    {
      "index": 2,
      "title": "An identifier fully served in the cited file never vetoes",
      "files": [
        "src/repoach/review/refuter_evidence.py",
        "tests/unit/test_refuter_veto_names_real_identifiers.py"
      ],
      "action": "Classify each searched identifier as unserved, partial (a hit in the cited file cut, or the cited file holds none and a hit elsewhere cut) or partial_elsewhere, and expose partial_in_cited and partial_elsewhere on EvidenceBundle.",
      "commit_message": "fix(refuter): judge a partial identifier by the cited file",
      "done_when": "pytest tests/unit/test_refuter_veto_names_real_identifiers.py -k 'cited_file or classified' passes",
      "unit_tests": [
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_identifier_fully_served_in_the_cited_file_does_not_veto",
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_identifier_absent_from_the_cited_file_and_cut_elsewhere_still_vetoes",
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_cut_inside_the_cited_file_is_classified_in_cited"
      ]
    },
    {
      "index": 3,
      "title": "The deferral reason names the rule that held",
      "files": [
        "src/repoach/review/refuter.py",
        "tests/unit/test_refuter_defers_unseen.py",
        "tests/integration/test_refuter_defers_unserved_end_to_end.py"
      ],
      "action": "Veto a refutation only on EvidenceBundle.partial, with EVIDENCE_PARTLY_SERVED_IN_CITED or EVIDENCE_PARTLY_SERVED_OUTSIDE, and reword EVIDENCE_NOT_SERVED to 'identifier not served: <ids>'.",
      "commit_message": "fix(refuter): name the rule that defers a verdict",
      "done_when": "pytest tests/unit/test_refuter_defers_unseen.py tests/unit/test_refuter_veto_names_real_identifiers.py passes",
      "unit_tests": [
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_deferral_reason_names_the_rule_that_held",
        "tests/unit/test_refuter_defers_unseen.py::test_refutation_resting_on_a_partly_served_identifier_is_deferred",
        "tests/unit/test_refuter_defers_unseen.py::test_hits_cut_by_the_budget_defer_without_calling_the_judge",
        "tests/unit/test_refuter_defers_unseen.py::test_unserved_reason_is_a_closed_set_template"
      ]
    },
    {
      "index": 4,
      "title": "Re-judge a needs-judgement stamp when the rules or the context change",
      "files": [
        "src/repoach/review/head_reverify.py",
        "tests/unit/test_refuter_veto_names_real_identifiers.py",
        "tests/integration/test_refuter_veto_replays_pr260.py"
      ],
      "action": "Stamp a needs_judgement outcome with judgement_key(context_tokens) and reuse it at the same head only under the same key; add the context_tokens parameter to reverify_older_findings; replay PR #260 findings 41 and 66 end to end through reverify_older_findings.",
      "commit_message": "fix(reverify): re-judge needs judgement when the rules change",
      "done_when": "pytest tests/unit/test_refuter_veto_names_real_identifiers.py -k 'needs_judgement or stamp' passes",
      "unit_tests": [
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_needs_judgement_is_reused_only_under_the_same_key",
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_needs_judgement_is_re_judged_at_a_new_head",
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_needs_judgement_is_re_judged_when_the_context_changes",
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_needs_judgement_is_re_judged_on_a_rule_version_bump",
        "tests/unit/test_refuter_veto_names_real_identifiers.py::test_stamp_written_before_the_rule_version_is_re_judged"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_refuter_veto_replays_pr260.py::test_measured_refutation_settles_the_finding_at_the_next_head",
    "tests/integration/test_refuter_defers_unserved_end_to_end.py::test_unserved_identifier_is_deferred_through_the_review_without_a_judge_call"
  ]
}
```
