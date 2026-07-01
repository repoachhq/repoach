# SP-DELETE-CODER-RESPOND — delete the dead archive-verdict Coder path

**Status:** OPEN
**Redesign:** the "reviewer-persona rewrite" cleanup, reduced to its real
content (the 4 reviewer personas were already migrated to the findings
contract in earlier slices — only the legacy Coder path is left dead).
**Touches forbidden paths:** yes (`prompts/review/coder_0.4.0.md`) —
hand-shipped via PR, never bot-edited.

## Why

The CI Coder flipped onto the evidence-first findings path
(`Coder.respond_to_findings` → `coder_findings_0.1.0.md`) in #397, and
SP-DELETE-LEGACY-CODER (#400) deleted the legacy `run_coder_fix` subtree
but **left `Coder.respond` + `coder_0.4.0.md` in place** for "the
reviewer-persona rewrite chantier". A cartography of `prompts/review/`
shows that chantier is now empty of persona work: `architect_0.4.0.md`,
`sentinel_0.4.0.md`, `tester_0.4.0.md`, `scribe_0.5.0.md` already describe
the live contract (a `verdict` + inline `comments`; the "challenge with
evidence" / "final aggregation" passages they carry refer to the LIVE
evidence-challenge sentinel and the LIVE round-2 `retracted`-comment drop,
not the deleted machinery). The only dead residue is the legacy Coder
response path:

- `reviewer.Coder.respond` (the `reviews[]` + `challenge_report` /
  ACCEPT-CHALLENGE-DEFER shape) has **no production caller** — the live
  path is `respond_to_findings` (`coder_findings.py:532`). It is exercised
  only by two unit tests.
- `coder_0.4.0.md` is referenced **only** by `Coder.respond` (the
  `persona_filename` class attribute + the class docstring).
- `challenge_report` survives as a *shape* the shared parser still accepts
  (`_is_valid_coder_plan`, `_developer_response_has_fixes`) although
  nothing emits it anymore (`respond_to_findings` and `Developer.respond`
  both emit `fixes` only).

## Change

`prompts/review/coder_0.4.0.md` — **deleted**.

`src/ferova/review/reviewer.py`:
- Delete `Coder.respond` (the whole method, ~957-1099) and the
  `persona_filename = "coder_0.4.0.md"` class attribute.
- Rewrite the `Coder` class docstring to describe only
  `respond_to_findings` (drop the `challenge_report` / ACCEPT-CHALLENGE-
  DEFER / full-file-rewrite-from-reviews framing; keep the patch-discipline
  + `max_tokens` rationale, which still apply to the findings path).
- `_is_valid_coder_plan`: drop the `"challenge_report"` shape — a valid
  Coder/Developer payload now carries `fixes`. Rewrite its docstring
  (drop the SP-CODER-EVIDENCE-CHALLENGE two-shapes note).
- `_developer_response_has_fixes`: drop the
  `if "challenge_report" in parsed: return True` branch (the Developer
  never emitted it; it was the dead Coder path's shape).
- `_parse_fix_plan` docstring: drop the "`fixes` OR `challenge_report`"
  line (now `fixes` only).

`tests/unit/test_review_team.py`:
- Delete the two dead tests that call `coder.respond(...)`
  (`test_coder_respond_substitutes_spec_plan_placeholder`,
  `test_coder_respond_with_no_spec_plan_uses_placeholder`).
- Re-home their intent onto the live path: add
  `test_respond_to_findings_substitutes_spec_plan_placeholder` +
  `_uses_placeholder` asserting `{SPEC_PLAN}` substitution /
  fallback in `respond_to_findings`'s rendered prompt (the `coder_findings`
  persona). Coverage of the SPEC_PLAN wiring is preserved, just moved to
  the path that actually runs.

## Acceptance

- `prompts/review/` no longer contains `coder_0.4.0.md`; no code reference
  to it survives (`grep -rn coder_0.4.0 src tests` → empty).
- No reference to `Coder.respond` (the dead method) or `challenge_report`
  survives in `src/` (`grep -rn challenge_report src` → empty).
- Full `tests/unit` green; `ruff` + format + no-inline-comments +
  no-silent-except clean; zero inline comments / `# noqa`.

## Out of scope

- The per-reviewer `verdict` enum field on `ReviewerOutcome` — still live
  schema (parsed, recorded, displayed); retiring it is a separate
  schema-shaped decision, not this dead-code delete.
- 10b-4 (the `pr_merges.verdict` / `LEGACY_VERDICT_HEADER` schema
  migration) — a distinct cleanup.
</content>
