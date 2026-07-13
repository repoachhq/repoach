---
id: SP-PROMPT-PLACEHOLDER-ORDER
title: Inject the untrusted diff last so it cannot expand trusted placeholders
version: 0.1
status: draft
author: jfaye (Ferova audit 2026-07-13)
created: 2026-07-13
updated: 2026-07-13

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Inject the untrusted diff last so it cannot expand trusted placeholders

## Intent

Prompt rendering substitutes the untrusted PR diff into the persona
template BEFORE the trusted placeholders, so a hostile diff carrying
literal placeholder tokens gets them expanded inside the diff region —
letting attacker-controlled content forge prior-review, spec, or
dialogue context. Render trusted placeholders first and inject the
untrusted diff last so its tokens stay verbatim.

## Context

Audit 2026-07-13 finding M4. Three render sites share the flaw:

- `src/ferova/review/reviewer.py` `_render_prompt` lines 749-756:
  `.replace("{DIFF}", diff)` runs FIRST, then `{SPEC_PLAN}`,
  `{DIALOGUE_CONTEXT}`, `{RESOLVED_DISAGREEMENTS}`, `{PRIOR_REVIEW}`,
  `{ARCH_EDGES}` are `.replace`-d over the WHOLE string — including the
  just-inserted diff. A diff containing a literal `{PRIOR_REVIEW}` has
  it expanded.
- `Coder.respond_to_findings` lines 1042-1046: `.replace("{DIFF}", ...)`
  first, then `{FINDINGS_JSON}` and `{SPEC_PLAN}` over the whole
  string.
- `Developer.respond` lines 1680-1684: `.replace("{SPEC_PLAN}", ...)`
  first, then `{EXISTING_FILES}` / `{REPO_TREE}` — same order hazard for
  any untrusted content substituted before trusted tokens.

The already-safe pattern is `extra_prompt_section` (untrusted content
appended AFTER all placeholder substitution), which the fix should
generalize. These prompts feed the reviewer/coder/developer agents over
the proxy. Review-integrity change, not a merge-path change.

## Goals

- G1: at every render site, all TRUSTED placeholders are substituted
  first; the UNTRUSTED diff (and any other untrusted blob, e.g. finding
  bodies) is injected LAST, so untrusted content can never trigger a
  further placeholder expansion.
- G2: a literal placeholder token appearing in untrusted content is
  rendered verbatim in the final prompt, not expanded.
- G3: the ordering fix is applied consistently across
  `reviewer._render_prompt`, `Coder.respond_to_findings`, and
  `Developer.respond`.

## Non-Goals

- NG1: no change to the persona template files under `prompts/review/*`
  (path whitelist forbids bot edits there; the ordering fix is in
  Python).
- NG2: no change to what context the trusted placeholders carry — only
  substitution ORDER.
- NG3: no general templating-engine migration; the minimal fix is
  reorder + inject-last (or neutralize tokens in untrusted content).

## Assumptions

- A1: trusted placeholders (`{SPEC_PLAN}`, `{DIALOGUE_CONTEXT}`,
  `{RESOLVED_DISAGREEMENTS}`, `{PRIOR_REVIEW}`, `{ARCH_EDGES}`,
  `{FINDINGS_JSON}`, `{EXISTING_FILES}`, `{REPO_TREE}`) are built from
  ferova-controlled data, not from the diff; the diff and reviewer/
  finding bodies are the untrusted inputs.
- A2: injecting the diff last is behaviorally equivalent for a benign
  diff (no placeholder tokens present) — the rendered prompt is
  identical, so no persona/quality regression.

## Interface

N/A (in-place fix, no public signature change).

## Behavior

### Nominal

Benign diff with no placeholder tokens: rendered prompt is byte-for-
byte what it is today (all placeholders filled, diff in place).

### Edge cases

- Diff contains a literal `{PRIOR_REVIEW}` / `{SPEC_PLAN}` /
  `{FINDINGS_JSON}` token: after the fix the token appears verbatim in
  the diff region of the final prompt — no trusted content is injected
  there.
- A trusted placeholder's own content happens to contain a literal
  `{DIFF}` token: because the diff is injected LAST by a single final
  substitution, that token in trusted content is also left verbatim (or
  is explicitly the last replacement) — define the order so no trusted
  block can re-expand into another.

### Failure scenarios

- Untrusted content crafted to forge a "prior APPROVE" or a fake spec
  section → neutralized: the tokens never expand, so no forged context
  reaches the agent. Fail closed against prompt injection.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `reviewer.py` (owned by an existing spec, the review arc); no new
  cross-owner import.
- New / changed coupling, cycles, or shared state: none. The fix aligns
  all three render sites on the safe `extra_prompt_section` ordering
  already present in the module.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — `reviewer._render_prompt` given a diff containing a
  literal `{PRIOR_REVIEW}` token renders it verbatim (the real prior-
  review block does NOT appear inside the diff region); analogous unit
  checks for `Coder.respond_to_findings` (`{SPEC_PLAN}` token in the
  diff) and `Developer.respond` (a placeholder token in untrusted
  input).
- [ ] AC2 (INTEGRATION): drive a reviewer render end-to-end through the
  reviewer entrypoint (real persona template loaded from
  `prompts/review/`, trusted context supplied) with a hostile diff that
  embeds `{PRIOR_REVIEW}` and `{SPEC_PLAN}` tokens; capture the final
  prompt string and assert the injected tokens are present verbatim and
  the genuine trusted blocks appear exactly once, outside the diff
  region.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_reviewer.py::test_untrusted_diff_tokens_not_expanded`
  and
  `tests/unit/test_reviewer.py::test_coder_and_developer_render_inject_untrusted_last`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
