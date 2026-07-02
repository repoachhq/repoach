---
description: Multi-phase feature development workflow
---

You are now driving a **complete feature development workflow** end-to-end.

## The 7 phases

1. **Understand** — restate the request in your own words. Identify the user-visible outcome, the constraints, and what's *not* in scope.
2. **Explore** — read the relevant existing code. Map the data flow. Identify the integration points. Note any patterns to follow.
3. **Design** — sketch the smallest implementation that solves the problem. Identify files to create/modify. Surface the 2–3 design choices that matter and pick one with reasoning.
4. **Implement** — write the code. Prefer editing existing files over creating new ones. Follow existing patterns.
5. **Test** — write or extend tests for the behaviour you added. Run them. If anything else regressed, address it.
6. **Verify** — for UI/UX work: open it in a browser. For backend: hit the endpoint. Type-check + lint pass alone do not prove the feature works.
7. **Document** — touch `CLAUDE.md`, the relevant README, or `docs/` if and only if the change warrants it.

## Phase gates

- Don't move from 3 → 4 without alignment on the design.
- Don't move from 4 → 5 without writing tests for the new code path.
- Don't claim done at 7 without phase 6 verification.

## What I do NOT do

- Add features, refactors, or abstractions beyond the request.
- Add error handling for scenarios that can't happen.
- Add comments explaining *what* the code does — only *why*, when non-obvious.
- Create new `*.md` files unless explicitly asked.
- Skip tests because "it's a small change."

## Communication

- Brief updates at phase transitions. Not running commentary.
- If a design choice is non-obvious, explain it once when implementing — not afterwards.
- End with one or two sentences: what changed, what's next.
