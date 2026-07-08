# SP-CLAIM-TYPE-ROUTING — Content-based claim typing and fail-closed routing

Add a pure content-based classifier in findings_bridge that overrides the per-lens default claim type using keyword cues, promote spec_gap to a judged claim type so every enum value has a route, and make gather_merge_facts fail closed by surfacing blocking findings whose claim type has no verifier or that sit PROPOSED at head as a new blocking_unverified fact that refuses the merge.

## Step 1 — Add classify_claim_type and wire it into comment_to_finding

- **Files**: `src/ferova/review/findings_bridge.py`, `tests/unit/test_findings_bridge.py`
- **Action**: In src/ferova/review/findings_bridge.py, add a pure function classify_claim_type(body: str, role: BotRole) -> ClaimType that scans the comment body for keyword/phrase cues (missing_test: existing trigger phrases like 'no test for', 'missing test'; missing_docstring: 'missing docstring', 'no docstring'; lint_convention: 'lint', 'convention', 'style nit'; broken_behavior: 'race', 'deadlock', 'crash', 'broken', 'bug'; spec_gap: 'spec gap', 'not in spec', 'specification gap'; security: 'security', 'vulnerability', 'injection', 'auth bypass'; design: 'design', 'architecture', 'pattern'). Apply a documented priority order (mechanical types before judged types). When no cue fires, return LENS_DEFAULT_CLAIM_TYPE.get(role, ClaimType.DESIGN). Update comment_to_finding to call classify_claim_type(comment.body, role) instead of indexing LENS_DEFAULT_CLAIM_TYPE directly. Add tests in tests/unit/test_findings_bridge.py: test_content_cues_override_lens_default (security-worded Tester comment -> security; test-worded Sentinel comment -> missing_test) and test_no_cue_keeps_lens_default (cue-free comments keep today's lens mapping).
- **Commit**: `feat(review): content-based claim type classifier in findings bridge`
- **Done when**: pytest tests/unit/test_findings_bridge.py::test_content_cues_override_lens_default tests/unit/test_findings_bridge.py::test_no_cue_keeps_lens_default passes
- **Unit tests**: `tests/unit/test_findings_bridge.py::test_content_cues_override_lens_default`, `tests/unit/test_findings_bridge.py::test_no_cue_keeps_lens_default`

## Step 2 — Promote spec_gap to a judged claim type

- **Files**: `src/ferova/review/refuter.py`, `tests/unit/test_review_refuter.py`
- **Action**: In src/ferova/review/refuter.py, add ClaimType.SPEC_GAP to the JUDGED_CLAIM_TYPES frozenset so spec_gap findings are routed to the refuter path (no longer a dead enum value). Create tests/unit/test_review_refuter.py with test_spec_gap_is_judged that asserts ClaimType.SPEC_GAP is in JUDGED_CLAIM_TYPES and that a proposed spec_gap finding is included in the judged_targets list built by judge_findings_for_pr (verified via a stub judge_factory that records which findings it was asked to judge).
- **Commit**: `feat(review): route spec_gap findings through the refuter`
- **Done when**: pytest tests/unit/test_review_refuter.py::test_spec_gap_is_judged passes
- **Unit tests**: `tests/unit/test_review_refuter.py::test_spec_gap_is_judged`

## Step 3 — Add blocking_unverified fact and fail-closed gate

- **Files**: `src/ferova/review/merge_gate.py`, `tests/unit/test_merge_gate.py`
- **Action**: In src/ferova/review/merge_gate.py: (a) add a blocking_unverified: list[str] field to MergeFacts; (b) in gather_merge_facts, for each blocking finding that is not settled, check whether its claim_type is in _MECHANICAL_TYPES or _JUDGED_TYPES — if not, append a reason string naming the finding id and its claim_type to blocking_unverified; also append a reason for any blocking finding whose status is PROPOSED at head (it was never verified); (c) in compute_merge_decision, when facts.blocking_unverified is non-empty, append a refusal reason 'N unverified blocking finding(s): ...' and set merge=False. Add tests/unit/test_merge_gate.py::test_blocking_unverified_refuses_merge that constructs a MergeFacts with a non-empty blocking_unverified list and asserts compute_merge_decision returns merge=False with a reason naming the finding.
- **Commit**: `feat(review): fail-closed merge gate for unverified blocking findings`
- **Done when**: pytest tests/unit/test_merge_gate.py::test_blocking_unverified_refuses_merge passes
- **Unit tests**: `tests/unit/test_merge_gate.py::test_blocking_unverified_refuses_merge`

## Step 4 — Add integration test for end-to-end claim type routing

- **Files**: `tests/integration/test_claim_type_routing.py`
- **Action**: Create tests/integration/test_claim_type_routing.py with an integration test that exercises the full pipeline: build a comment whose body triggers a content cue (e.g. 'this branch can drop the lock — race under concurrent writes'), run it through comment_to_finding to confirm classify_claim_type assigns broken_behavior (not the Tester lens default missing_test), then feed the resulting finding into gather_merge_facts and compute_merge_decision to confirm a blocking finding with an unmapped claim type or PROPOSED status produces merge=False with a blocking_unverified reason. The test must import the real modules (no heavy mocking) and assert the end-to-end routing outcome.
- **Commit**: `test(review): integration test for content-based claim type routing`
- **Done when**: pytest tests/integration/test_claim_type_routing.py passes
- **Unit tests**: `tests/integration/test_claim_type_routing.py::test_content_cue_routes_to_correct_verifier_and_gate`

## Integration tests

- `tests/integration/test_claim_type_routing.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-CLAIM-TYPE-ROUTING",
  "title": "Content-based claim typing and fail-closed routing",
  "summary": "Add a pure content-based classifier in findings_bridge that overrides the per-lens default claim type using keyword cues, promote spec_gap to a judged claim type so every enum value has a route, and make gather_merge_facts fail closed by surfacing blocking findings whose claim type has no verifier or that sit PROPOSED at head as a new blocking_unverified fact that refuses the merge.",
  "steps": [
    {
      "index": 1,
      "title": "Add classify_claim_type and wire it into comment_to_finding",
      "files": [
        "src/ferova/review/findings_bridge.py",
        "tests/unit/test_findings_bridge.py"
      ],
      "action": "In src/ferova/review/findings_bridge.py, add a pure function classify_claim_type(body: str, role: BotRole) -> ClaimType that scans the comment body for keyword/phrase cues (missing_test: existing trigger phrases like 'no test for', 'missing test'; missing_docstring: 'missing docstring', 'no docstring'; lint_convention: 'lint', 'convention', 'style nit'; broken_behavior: 'race', 'deadlock', 'crash', 'broken', 'bug'; spec_gap: 'spec gap', 'not in spec', 'specification gap'; security: 'security', 'vulnerability', 'injection', 'auth bypass'; design: 'design', 'architecture', 'pattern'). Apply a documented priority order (mechanical types before judged types). When no cue fires, return LENS_DEFAULT_CLAIM_TYPE.get(role, ClaimType.DESIGN). Update comment_to_finding to call classify_claim_type(comment.body, role) instead of indexing LENS_DEFAULT_CLAIM_TYPE directly. Add tests in tests/unit/test_findings_bridge.py: test_content_cues_override_lens_default (security-worded Tester comment -> security; test-worded Sentinel comment -> missing_test) and test_no_cue_keeps_lens_default (cue-free comments keep today's lens mapping).",
      "commit_message": "feat(review): content-based claim type classifier in findings bridge",
      "done_when": "pytest tests/unit/test_findings_bridge.py::test_content_cues_override_lens_default tests/unit/test_findings_bridge.py::test_no_cue_keeps_lens_default passes",
      "unit_tests": [
        "tests/unit/test_findings_bridge.py::test_content_cues_override_lens_default",
        "tests/unit/test_findings_bridge.py::test_no_cue_keeps_lens_default"
      ]
    },
    {
      "index": 2,
      "title": "Promote spec_gap to a judged claim type",
      "files": [
        "src/ferova/review/refuter.py",
        "tests/unit/test_review_refuter.py"
      ],
      "action": "In src/ferova/review/refuter.py, add ClaimType.SPEC_GAP to the JUDGED_CLAIM_TYPES frozenset so spec_gap findings are routed to the refuter path (no longer a dead enum value). Create tests/unit/test_review_refuter.py with test_spec_gap_is_judged that asserts ClaimType.SPEC_GAP is in JUDGED_CLAIM_TYPES and that a proposed spec_gap finding is included in the judged_targets list built by judge_findings_for_pr (verified via a stub judge_factory that records which findings it was asked to judge).",
      "commit_message": "feat(review): route spec_gap findings through the refuter",
      "done_when": "pytest tests/unit/test_review_refuter.py::test_spec_gap_is_judged passes",
      "unit_tests": [
        "tests/unit/test_review_refuter.py::test_spec_gap_is_judged"
      ]
    },
    {
      "index": 3,
      "title": "Add blocking_unverified fact and fail-closed gate",
      "files": [
        "src/ferova/review/merge_gate.py",
        "tests/unit/test_merge_gate.py"
      ],
      "action": "In src/ferova/review/merge_gate.py: (a) add a blocking_unverified: list[str] field to MergeFacts; (b) in gather_merge_facts, for each blocking finding that is not settled, check whether its claim_type is in _MECHANICAL_TYPES or _JUDGED_TYPES — if not, append a reason string naming the finding id and its claim_type to blocking_unverified; also append a reason for any blocking finding whose status is PROPOSED at head (it was never verified); (c) in compute_merge_decision, when facts.blocking_unverified is non-empty, append a refusal reason 'N unverified blocking finding(s): ...' and set merge=False. Add tests/unit/test_merge_gate.py::test_blocking_unverified_refuses_merge that constructs a MergeFacts with a non-empty blocking_unverified list and asserts compute_merge_decision returns merge=False with a reason naming the finding.",
      "commit_message": "feat(review): fail-closed merge gate for unverified blocking findings",
      "done_when": "pytest tests/unit/test_merge_gate.py::test_blocking_unverified_refuses_merge passes",
      "unit_tests": [
        "tests/unit/test_merge_gate.py::test_blocking_unverified_refuses_merge"
      ]
    },
    {
      "index": 4,
      "title": "Add integration test for end-to-end claim type routing",
      "files": [
        "tests/integration/test_claim_type_routing.py"
      ],
      "action": "Create tests/integration/test_claim_type_routing.py with an integration test that exercises the full pipeline: build a comment whose body triggers a content cue (e.g. 'this branch can drop the lock — race under concurrent writes'), run it through comment_to_finding to confirm classify_claim_type assigns broken_behavior (not the Tester lens default missing_test), then feed the resulting finding into gather_merge_facts and compute_merge_decision to confirm a blocking finding with an unmapped claim type or PROPOSED status produces merge=False with a blocking_unverified reason. The test must import the real modules (no heavy mocking) and assert the end-to-end routing outcome.",
      "commit_message": "test(review): integration test for content-based claim type routing",
      "done_when": "pytest tests/integration/test_claim_type_routing.py passes",
      "unit_tests": [
        "tests/integration/test_claim_type_routing.py::test_content_cue_routes_to_correct_verifier_and_gate"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_claim_type_routing.py"
  ]
}
```
