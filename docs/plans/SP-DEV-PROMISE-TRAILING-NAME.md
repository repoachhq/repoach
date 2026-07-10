# SP-DEV-PROMISE-TRAILING-NAME — Unify presence, fail fan-out drift in-loop

One shared promised_present predicate (word-boundary trailing-name match, class-nesting tolerant) fixes the substring bug and gives both gates P1 for free (step 1); the step gate then refuses a fan-out (P2) drift retryably with named feedback and can see class-nested delivered methods (step 2); an integration test drives the reconcile + step-gate + self-verify path end-to-end (step 3). Hand-authored: the Planner backend timed out (transient proxy flakiness), and the grounding workflow gave exact anchors. Every step stays well under the density cap; the integration AC is scoped (src-touching). No stubs — the Developer loop and the compliance judge are truthful boundary fakes.

## Step 1 — promised_present predicate (word-boundary, class-tolerant)

- **Files**: `src/ferova/review/spec_gate.py`, `tests/unit/test_spec_gate.py`
- **Action**: In src/ferova/review/spec_gate.py add `import re` and a function promised_present(repo_root: Path, selector: str) -> bool: resolve the file from the first "::" segment; read the source; take the trailing function name as the last "::" segment with any "[param]" stripped; return True iff re.search(r"(?m)^\s*def\s+" + re.escape(name) + r"\s*\(", source) matches — a word-boundary `def NAME(` at any indentation (flat or class-nested), regardless of intermediate class segments. Make the existing selector_present (spec_gate.py:100) delegate to promised_present so every caller (the merge gate and the self-verify unit-missing check at devagent_selfverify.py:277) gains the same P1 tolerance and the substring-bug fix (previously `f"def {name}" in source` at spec_gate.py:134 wrongly matched `def test_foobar` for promise `test_foo`, and required every intermediate `class {cls}`). Add tests/unit/test_spec_gate.py::test_promised_present_matches_class_nested_method: write a tmp file `class TestBar:\n    def test_foo(self):\n        assert True` and assert promised_present resolves True for both `<file>::test_foo` and `<file>::TestBaz::test_foo`. Add tests/unit/test_spec_gate.py::test_promised_present_word_boundary: a file whose only def is `def test_foobar(self): ...` is NOT satisfied by promise `<file>::test_foo` (red-before proves the fix), and a file with no matching def is not satisfied.
- **Commit**: `feat(review): trailing-name promised_present predicate`
- **Done when**: pytest tests/unit/test_spec_gate.py::test_promised_present_matches_class_nested_method tests/unit/test_spec_gate.py::test_promised_present_word_boundary passes
- **Unit tests**: `tests/unit/test_spec_gate.py::test_promised_present_matches_class_nested_method`, `tests/unit/test_spec_gate.py::test_promised_present_word_boundary`

## Step 2 — Step gate refuses fan-out in-loop and sees class methods

- **Files**: `src/ferova/review/dev_runner.py`, `tests/unit/test_dev_runner_promise.py`
- **Action**: In src/ferova/review/dev_runner.py make three changes. (a) _test_function_names_in_file (dev_runner.py:92-114): change the regex at line 114 from `^def\s+(test_\w+)` to `(?m)^\s*def\s+(test_\w+)\s*\(` so indented class-nested delivered methods are discoverable for feedback listing. (b) Fix the latent bug at dev_runner.py:1500-1504 where the promised-name extraction `s.split("::",1)[1].split("::",1)[0]` returns the CLASS segment for a class-scoped promise — take the LAST "::" segment as the function name instead. (c) In the reconcile branch (dev_runner.py:1478-1558), after the touched-file guard, when a promised selector's trailing name is not promised_present (import it from .spec_gate) in the touched file, REFUSE retryably (append feedback + `continue`, mirroring the touched-file-guard retry at dev_runner.py:1482-1496) instead of accepting: the feedback NAMES the absent promised selectors AND LISTS the delivered test function names found in the file via _test_function_names_in_file, with the instruction "add a test named exactly <name>, or correct the plan promise". Add tests/unit/test_dev_runner_promise.py::test_step_gate_refuses_fanout_naming_selectors: with a truthful scripted fake Developer that writes the touched file as two classes with differently-named methods against two promised flat selectors, assert the step-gate feedback string contains both absent selector names and at least one delivered method name; then a second attempt that adds tests named exactly as the two promises passes the gate. Add tests/unit/test_dev_runner_promise.py::test_step_gate_and_self_verify_agree: a P1 class-nested delivery (method name equals the promised trailing name) is accepted at the step gate AND passes run_self_verify's presence check (pass a truthful boundary-fake gate_judge returning compliant so no real LLM runs); the P2 fan-out shape is refused at the STEP gate, not only at terminal self-verify.
- **Commit**: `feat(dev_runner): refuse promised-test fan-out retryably at the step gate`
- **Done when**: pytest tests/unit/test_dev_runner_promise.py::test_step_gate_refuses_fanout_naming_selectors tests/unit/test_dev_runner_promise.py::test_step_gate_and_self_verify_agree passes
- **Unit tests**: `tests/unit/test_dev_runner_promise.py::test_step_gate_refuses_fanout_naming_selectors`, `tests/unit/test_dev_runner_promise.py::test_step_gate_and_self_verify_agree`

## Step 3 — End-to-end fan-out reconcile integration test

- **Files**: `tests/integration/test_promise_fanout_reconcile.py`
- **Action**: Create tests/integration/test_promise_fanout_reconcile.py::test_fanout_drift_refused_in_loop_then_self_corrects driving the reconcile + step-gate + self-verify path in a throwaway git repo (tmp_path, real git; helpers mirroring tests/integration/test_dev_runner_promise_delivery.py if present, else a local _git helper). Build a two-step-ish fake plan whose step promises two flat unit selectors in one test file. Drive the Developer step loop with a truthful scripted fake loop (per the _ScriptedLoop pattern used in the promise-delivery integration tests): its FIRST scripted write delivers the promised file as two classes with differently-named methods (the fan-out); assert the step gate refuses in-loop and the recorded feedback names both absent selectors; its SECOND scripted write adds tests named exactly as the two promises; assert the step passes and a subsequent run_self_verify (with a truthful boundary-fake gate_judge) finds no missing promised units. Hermetic: no network, no real LLM, no `.env` reliance.
- **Commit**: `test(dev_runner): end-to-end fan-out refuse-then-self-correct integration test`
- **Done when**: pytest tests/integration/test_promise_fanout_reconcile.py::test_fanout_drift_refused_in_loop_then_self_corrects passes
- **Unit tests**: `tests/integration/test_promise_fanout_reconcile.py::test_fanout_drift_refused_in_loop_then_self_corrects`

## Step 4 — Reconcile the SP-DEV-PROMISE-DELIVERY AC3 test with the new behavior

- **Files**: `tests/unit/test_review_plan_executor.py`
- **Action**: The SP-DEV-PROMISE-DELIVERY AC3 test tests/unit/test_review_plan_executor.py::TestPromisedTestGateG1G2::test_ambiguous_drift_keeps_reconciled_accept asserts that an ambiguous drift with ABSENT promised names (promises test_a/test_b, delivers test_x/test_y) KEEPS a reconciled-accept — the exact accept-then-die-at-self-verify behavior SP-DEV-PROMISE-TRAILING-NAME reverses. Rename it to test_ambiguous_drift_absent_names_refused and update its body to assert the step now REFUSES the drift (outcome.ok is False) and that the retry feedback names the absent selectors test_a and test_b; update the docstring to cite SP-DEV-PROMISE-TRAILING-NAME (was SP-DEV-PROMISE-DELIVERY AC3). Leave the other TestPromisedTestGateG1G2 methods unchanged — they were verified still green on the impl branch.
- **Commit**: `test(review): AC3 test reflects the new refuse-fan-out behavior`
- **Done when**: pytest tests/unit/test_review_plan_executor.py::TestPromisedTestGateG1G2::test_ambiguous_drift_absent_names_refused passes
- **Unit tests**: `tests/unit/test_review_plan_executor.py::TestPromisedTestGateG1G2::test_ambiguous_drift_absent_names_refused`

## Integration tests

- `tests/integration/test_promise_fanout_reconcile.py::test_fanout_drift_refused_in_loop_then_self_corrects`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-DEV-PROMISE-TRAILING-NAME",
  "title": "Unify presence, fail fan-out drift in-loop",
  "summary": "One shared promised_present predicate (word-boundary trailing-name match, class-nesting tolerant) fixes the substring bug and gives both gates P1 for free (step 1); the step gate then refuses a fan-out (P2) drift retryably with named feedback and can see class-nested delivered methods (step 2); an integration test drives the reconcile + step-gate + self-verify path end-to-end (step 3). No stubs - the Developer loop and the compliance judge are truthful boundary fakes.",
  "steps": [
    {
      "index": 1,
      "title": "promised_present predicate (word-boundary, class-tolerant)",
      "files": [
        "src/ferova/review/spec_gate.py",
        "tests/unit/test_spec_gate.py"
      ],
      "action": "In src/ferova/review/spec_gate.py add `import re` and a function promised_present(repo_root: Path, selector: str) -> bool: resolve the file from the first \"::\" segment; read the source; take the trailing function name as the last \"::\" segment with any \"[param]\" stripped; return True iff re.search(r\"(?m)^\\s*def\\s+\" + re.escape(name) + r\"\\s*\\(\", source) matches - a word-boundary `def NAME(` at any indentation (flat or class-nested), regardless of intermediate class segments. Make the existing selector_present (spec_gate.py:100) delegate to promised_present so every caller (the merge gate and the self-verify unit-missing check at devagent_selfverify.py:277) gains the same P1 tolerance and the substring-bug fix (previously `f\"def {name}\" in source` at spec_gate.py:134 wrongly matched `def test_foobar` for promise `test_foo`, and required every intermediate `class {cls}`). Add tests/unit/test_spec_gate.py::test_promised_present_matches_class_nested_method: write a tmp file `class TestBar:\\n    def test_foo(self):\\n        assert True` and assert promised_present resolves True for both `<file>::test_foo` and `<file>::TestBaz::test_foo`. Add tests/unit/test_spec_gate.py::test_promised_present_word_boundary: a file whose only def is `def test_foobar(self): ...` is NOT satisfied by promise `<file>::test_foo` (red-before proves the fix), and a file with no matching def is not satisfied.",
      "commit_message": "feat(review): trailing-name promised_present predicate",
      "done_when": "pytest tests/unit/test_spec_gate.py::test_promised_present_matches_class_nested_method tests/unit/test_spec_gate.py::test_promised_present_word_boundary passes",
      "unit_tests": [
        "tests/unit/test_spec_gate.py::test_promised_present_matches_class_nested_method",
        "tests/unit/test_spec_gate.py::test_promised_present_word_boundary"
      ]
    },
    {
      "index": 2,
      "title": "Step gate refuses fan-out in-loop and sees class methods",
      "files": [
        "src/ferova/review/dev_runner.py",
        "tests/unit/test_dev_runner_promise.py"
      ],
      "action": "In src/ferova/review/dev_runner.py make three changes. (a) _test_function_names_in_file (dev_runner.py:92-114): change the regex at line 114 from `^def\\s+(test_\\w+)` to `(?m)^\\s*def\\s+(test_\\w+)\\s*\\(` so indented class-nested delivered methods are discoverable for feedback listing. (b) Fix the latent bug at dev_runner.py:1500-1504 where the promised-name extraction `s.split(\"::\",1)[1].split(\"::\",1)[0]` returns the CLASS segment for a class-scoped promise - take the LAST \"::\" segment as the function name instead. (c) In the reconcile branch (dev_runner.py:1478-1558), after the touched-file guard, when a promised selector's trailing name is not promised_present (import it from .spec_gate) in the touched file, REFUSE retryably (append feedback + `continue`, mirroring the touched-file-guard retry at dev_runner.py:1482-1496) instead of accepting: the feedback NAMES the absent promised selectors AND LISTS the delivered test function names found in the file via _test_function_names_in_file, with the instruction \"add a test named exactly <name>, or correct the plan promise\". Add tests/unit/test_dev_runner_promise.py::test_step_gate_refuses_fanout_naming_selectors: with a truthful scripted fake Developer that writes the touched file as two classes with differently-named methods against two promised flat selectors, assert the step-gate feedback string contains both absent selector names and at least one delivered method name; then a second attempt that adds tests named exactly as the two promises passes the gate. Add tests/unit/test_dev_runner_promise.py::test_step_gate_and_self_verify_agree: a P1 class-nested delivery (method name equals the promised trailing name) is accepted at the step gate AND passes run_self_verify's presence check (pass a truthful boundary-fake gate_judge returning compliant so no real LLM runs); the P2 fan-out shape is refused at the STEP gate, not only at terminal self-verify.",
      "commit_message": "feat(dev_runner): refuse promised-test fan-out retryably at the step gate",
      "done_when": "pytest tests/unit/test_dev_runner_promise.py::test_step_gate_refuses_fanout_naming_selectors tests/unit/test_dev_runner_promise.py::test_step_gate_and_self_verify_agree passes",
      "unit_tests": [
        "tests/unit/test_dev_runner_promise.py::test_step_gate_refuses_fanout_naming_selectors",
        "tests/unit/test_dev_runner_promise.py::test_step_gate_and_self_verify_agree"
      ]
    },
    {
      "index": 3,
      "title": "End-to-end fan-out reconcile integration test",
      "files": [
        "tests/integration/test_promise_fanout_reconcile.py"
      ],
      "action": "Create tests/integration/test_promise_fanout_reconcile.py::test_fanout_drift_refused_in_loop_then_self_corrects driving the reconcile + step-gate + self-verify path in a throwaway git repo (tmp_path, real git; helpers mirroring tests/integration/test_dev_runner_promise_delivery.py if present, else a local _git helper). Build a fake plan whose step promises two flat unit selectors in one test file. Drive the Developer step loop with a truthful scripted fake loop (per the _ScriptedLoop pattern used in the promise-delivery integration tests): its FIRST scripted write delivers the promised file as two classes with differently-named methods (the fan-out); assert the step gate refuses in-loop and the recorded feedback names both absent selectors; its SECOND scripted write adds tests named exactly as the two promises; assert the step passes and a subsequent run_self_verify (with a truthful boundary-fake gate_judge) finds no missing promised units. Hermetic: no network, no real LLM, no `.env` reliance.",
      "commit_message": "test(dev_runner): end-to-end fan-out refuse-then-self-correct integration test",
      "done_when": "pytest tests/integration/test_promise_fanout_reconcile.py::test_fanout_drift_refused_in_loop_then_self_corrects passes",
      "unit_tests": [
        "tests/integration/test_promise_fanout_reconcile.py::test_fanout_drift_refused_in_loop_then_self_corrects"
      ]
    },
    {
      "index": 4,
      "title": "Reconcile the SP-DEV-PROMISE-DELIVERY AC3 test with the new behavior",
      "files": [
        "tests/unit/test_review_plan_executor.py"
      ],
      "action": "The SP-DEV-PROMISE-DELIVERY AC3 test tests/unit/test_review_plan_executor.py::TestPromisedTestGateG1G2::test_ambiguous_drift_keeps_reconciled_accept asserts that an ambiguous drift with ABSENT promised names (promises test_a/test_b, delivers test_x/test_y) KEEPS a reconciled-accept - the exact accept-then-die-at-self-verify behavior SP-DEV-PROMISE-TRAILING-NAME reverses. Rename it to test_ambiguous_drift_absent_names_refused and update its body to assert the step now REFUSES the drift (outcome.ok is False) and that the retry feedback names the absent selectors test_a and test_b; update the docstring to cite SP-DEV-PROMISE-TRAILING-NAME (was SP-DEV-PROMISE-DELIVERY AC3). Leave the other TestPromisedTestGateG1G2 methods unchanged - they were verified still green on the impl branch.",
      "commit_message": "test(review): AC3 test reflects the new refuse-fan-out behavior",
      "done_when": "pytest tests/unit/test_review_plan_executor.py::TestPromisedTestGateG1G2::test_ambiguous_drift_absent_names_refused passes",
      "unit_tests": [
        "tests/unit/test_review_plan_executor.py::TestPromisedTestGateG1G2::test_ambiguous_drift_absent_names_refused"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_promise_fanout_reconcile.py::test_fanout_drift_refused_in_loop_then_self_corrects"
  ]
}
```
