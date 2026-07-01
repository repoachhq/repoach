# DEVAGENT — a real coding agent (the Developer block)

Umbrella for the **SP-DEVAGENT-\*** arc. Turns the Developer from a one-shot
JSON generator into a genuine autonomous coding agent: it reads a governed spec,
decomposes it, **authors** the code, **runs the tests**, **verifies the spec is
met itself**, iterates, and only then hands a working result to the 4 reviewers.

## Why (the gap)

The machinery is mostly already here, but the Developer is wired in a crippled
topology: it calls `AgentLoop.run_oneshot()` (zero tools, one turn → one JSON
payload) and all the real work (writing files, running tests, verifying) is
bolted on **externally** in `dev_runner`. Yet the multi-turn tool-using loop
(`AgentLoop.run(tools=…)`) already exists and is proven — the **Planner** uses it
with read-only tools. So this arc is a **rewire + four capability gaps**, not a
rebuild.

Reused as-is: the brain (`AgentLoop` → proxy gateway → chains + `claude_code`,
SONNET tier), governed-spec ingestion (`owns`/`depends_on`, frontmatter, the
`arch check` edge-honesty gate), and the verification gates (`run_pytest`,
`run_ruff_gate`, the path whitelist `is_path_allowed`, commit-per-step, push→PR)
in `dev_runner` / `coder_loop`.

## Target pipeline

```
governed spec (owns/depends_on)
  → DECOMPOSE (always) → ordered governed sub-specs (their owns partition the parent's)
      └─ for each sub-spec → AGENTIC LOOP  (AgentLoop.run + tools)
            read spec → explore → write/edit files → run tests + ruff → read results → iterate until green
      → SELF-VERIFY  (mechanical: promised AC selectors present + green + full suite + ruff)
                     (semantic: an LLM judge reads spec + diff → compliance verdict)
  → only if green AND judge-OK → push → PR → 4 reviewers
```

Brain: the system's chains (proxy failover) + `claude_code` backstop — wired via
the SONNET capability tier (the redundant CODER tier was retired,
SP-CODER-TIER-RETIRE-AGENT).

## Calibrated decisions (operator, 2026-06-27)

- **Self-verification = mechanical + LLM judge.** Both must pass before review.
- **Always decompose.** Every spec goes through the decomposition front-end; a
  small spec yields a single sub-spec (uniform pipeline).
- **Rewire in place.** Evolve the existing `Developer` (`run_oneshot` → `run` +
  tools) and `dev_runner`; do not greenfield a parallel module.

## Slices (governed specs, in order)

1. **SP-DEVAGENT-TOOLS** — the agent's author + verify tool surface:
   `write_file` / `edit_file` (anchored via `patch_apply`) + `run_tests` /
   `run_ruff`, sandboxed to the path whitelist, mirroring `planner_tools.py`
   (jailed, capped, error-strings-never-raise). A pure additive leaf, tested in
   isolation.
2. **SP-DEVAGENT-LOOP** — rewire the Developer to `run(tools=…)`: the
   author→test→iterate multi-turn loop over slice-1 tools + the existing read
   tools. Replaces the one-shot. The spine.
3. **SP-DEVAGENT-SELFVERIFY** — the gate before handoff: mechanical (AC selectors
   present + green + full suite + ruff) **and** an LLM judge agent (reads spec +
   diff, verdicts semantic compliance).
4. **SP-DEVAGENT-DECOMPOSE** — the always-on front-end: governed spec → ordered
   governed sub-specs whose `owns` partition the parent's (honouring disjointness,
   `depends_on`, and edge-honesty); feeds the loop per sub-spec.
5. **SP-DEVAGENT-WIRE** — wire into `ferova develop` + the review handoff;
   commit-per-sub-spec; **remove the destructive revert-on-red** that wipes
   untracked files (a known `dev_runner` bug).

## Safety (carried from the autopilot incident)

- Tools are sandboxed to `is_path_allowed` (never `.github/workflows`,
  `prompts/review/*`, `.env*`, no traversal) and jailed to the repo root.
- Work on a feature branch, commit per sub-spec; **no** `git checkout .` / clean
  of untracked files on a red step (the bug that wiped specs).
- Bounded turns + a hard self-verify gate before any review/PR.
- Each slice ships through the factory with an adversarial pre-PR review (the
  pattern that caught real bugs across the chain-autopilot arc).

## Status

Arc opened 2026-06-27. Slice 1 (TOOLS) in progress.
