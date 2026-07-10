---
id: SP-DEV-PROMISE-TRAILING-NAME
title: Unify promised-test presence — match trailing name, fail fan-out drift in-loop
version: 0.1
status: approved
author: jfaye (SP-PLAN-QUALITY step-1 fan-out incident 2026-07-09; grounding workflow 2026-07-10; operator GO)
created: 2026-07-10
updated: 2026-07-10

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Unify promised-test presence — match trailing name, fail fan-out drift in-loop

## Intent

Two fixes to promised-test reconciliation, chosen over a semantic
auto-accept: (P1) a promised test node id is satisfied by its trailing
function name regardless of class nesting — mechanically, safely; and
(P2) a one-promise-to-many-differently-named-methods "fan-out" drift
is refused EARLY and RETRYABLY at the step gate (naming the exact
gap), never accepted-then-killed terminally at self-verify. No
semantic judge, no auto-accept — the session self-corrects in-loop.

## Context

SP-DEV-PROMISE-DELIVERY (#37-#39) reconciles a ONE-TO-ONE delivered/
promised test-name drift by mechanical rename, and deliberately
deferred semantic name matching (its NG2). Incident (2026-07-09,
SP-PLAN-QUALITY step 1, hand-fix commit `da10d29`): the session
delivered the two promised FLAT tests
(`test_rule_catalog_covers_every_validator`,
`test_catalog_renders_numbered_sentences`) as two test CLASSES with
seven differently-named methods (a 1→3 and 1→4 fan-out). The promised
flat node ids were absent at head, so the operator hand-appended them.

The grounding workflow (2026-07-10) found the real defect is a
GATE SPLIT, not an inability to accept fan-out:

- `selector_present` (`src/ferova/review/spec_gate.py:100-136`) is a
  raw SUBSTRING scan: `f"def {name}" in source` (spec_gate.py:134) —
  simultaneously TOO LOOSE (promise `test_foo` is satisfied by
  `def test_foobar(`) and TOO STRICT (it also requires every
  intermediate `class {cls}` to be present, spec_gate.py:136, so a
  class-scoped promise fails when the method is nested differently).
- The step gate (`dev_runner.py:1478-1558`) reconcile-ACCEPTS a
  touched-file fan-out drift with only a warning, while the TERMINAL
  `run_self_verify` (`devagent_selfverify.py:248,277-279`) re-checks
  presence and REFUSES — so P2 sails past the retryable in-loop gate
  and dies at session end, forcing a hand-fix.
- `_test_function_names_in_file` (`dev_runner.py:92-114`, regex `^def`
  at :114) sees only column-0 functions, so class-nested delivered
  methods are invisible to reconciliation.
- The compliance judge is already wired
  (`make_compliance_judge`, `devagent_selfverify.py:94-102`) but
  fails OPEN on unavailability; a fan-out-accepting judge would
  re-open the drift loophole on any proxy outage — rejected.

Class-nesting is a real recurring pattern (~22% of test functions,
11% of test files), so the P1 mechanical match earns its keep beyond
this one incident.

## Goals

- G1 (P1, mechanical): One `promised_present(repo_root, selector)`
  predicate matches the promised TRAILING function name via a
  word-boundary regex (`def\s+NAME\s*\(`) regardless of class nesting
  — a promise `file::test_foo` OR `file::TestBar::test_foo` is
  satisfied by any `def test_foo(` in the file. This LOOSENS the class
  requirement AND TIGHTENS the substring bug (`test_foo` no longer
  matches `def test_foobar(`).
- G2 (P2, loud + early + retryable): the STEP gate refuses a
  reconciled-green whose promised trailing name is absent from the
  touched file, with feedback that NAMES the absent selectors AND
  LISTS the delivered test functions in the file (including
  class-nested ones — `_test_function_names_in_file` must see indented
  `def test_`), instructing "add a test named exactly X, or correct
  the plan promise." Terminal death becomes an in-loop retry.
- G3 (no divergence): the step gate and `run_self_verify` call the
  SAME `promised_present` predicate, so nothing the step gate accepts
  can die at self-verify. The latent class-name-extraction bug at
  `dev_runner.py:1500-1504` (it reads the CLASS segment for a
  class-scoped promise) is fixed in passing.

## Non-Goals

- NG1: No semantic/judge auto-accept of fan-out drift (foolable by a
  trivial passing test; fail-open hazard on proxy outage; reverses
  SP-DEV-PROMISE-DELIVERY NG2). Fan-out fails loud and retryable.
- NG2: No change to the one-to-one mechanical rename path.
- NG3: No plan-form change (the upstream "promise a class or several
  named tests" fix is a separate SP-PLANNER-SELECTOR-CHECK-family
  candidate).

## Assumptions

- A1: A word-boundary `def\s+NAME\s*\(` regex over the file source is a
  sufficient, deterministic presence signal (same file-read shape as
  today's substring scan; no pytest run, no LLM).
- A2: The step gate's `changed` write-set (`dev_runner.py:1387`) and
  touched-file guard (`dev_runner.py:1482-1496`) already scope
  reconciliation to session-touched files; P2 detection reuses them.

## Behavior

- P1: a class-nested delivery whose method name equals the promised
  trailing name passes both gates with no rename — mechanically.
- P2: a fan-out (promised name absent, differently-named methods
  delivered in the touched file) is refused at the step gate with the
  named gap; the session adds a test named exactly as promised (or the
  operator corrects the plan) and the next attempt is green.
- Flat happy-path and one-to-one rename are unchanged.

## Acceptance Criteria

- AC1: `tests/unit/test_spec_gate.py::test_promised_present_matches_class_nested_method`
  — a real tmp file `class TestBar:\n    def test_foo(self): assert True`
  satisfies both `path::test_foo` and `path::TestBaz::test_foo`.
- AC2: `tests/unit/test_spec_gate.py::test_promised_present_word_boundary`
  — a file whose only def is `test_foobar` does NOT satisfy promise
  `test_foo` (red-before proves the substring-bug fix); a file lacking
  it fails too.
- AC3: `tests/unit/test_dev_runner_promise.py::test_step_gate_refuses_fanout_naming_selectors`
  — the incident shape (two flat promises; a truthful fake Developer
  writes the file as two classes with differently-named methods)
  yields step-gate feedback naming BOTH absent selectors and listing
  the delivered method names; a second attempt adding the two named
  tests goes green. No LLM.
- AC4: `tests/unit/test_dev_runner_promise.py::test_step_gate_and_self_verify_agree`
  — a P1 class-nested delivery accepted at the step gate also passes
  `run_self_verify`'s presence check; the P2 shape is refused at the
  STEP gate (not only terminally).
- AC5: Integration —
  `tests/integration/test_promise_fanout_reconcile.py::test_fanout_drift_refused_in_loop_then_self_corrects`
  — drive the reconcile + step-gate + self-verify path in a throwaway
  git repo with a truthful scripted fake loop: attempt 1 delivers the
  fan-out (refused in-loop, feedback names the selectors); attempt 2
  adds the promised names (green, self-verify passes). Hermetic: no
  network, no LLM, no `.env` reliance.
- AC6: Existing promise-reconciliation and self-verify suites stay
  green; the one-to-one rename and flat happy-path are unaffected.

## Open Questions

- OQ1: Should G2's feedback also suggest the PascalCase class the
  session likely created (the incident's flat→class promotion is
  mechanically detectable: `pascal(name) == delivered class name`)?
  Default: list delivered functions + the instruction; the class hint
  is a nice-to-have, not required.
