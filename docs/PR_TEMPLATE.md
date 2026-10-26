## Summary

<!-- What does this change do, and why? One or two sentences. -->

## Measured

<!--
One line per command you ran at the head commit. Each line must match
format:pr-measured-section: `- \`<command>\` → <expected>`. Replace the
examples below with what you actually ran — an indicative sentence with
no command ("tests pass") is not measurable and blocks the gate
(SP-AUTHOR-CLAIMS-ARE-MEASURED D8).
-->

Head: <sha7>
- `pytest -q tests/unit/test_example.py` → 3 passed
- `repoach arch check --base origin/develop` → ok
- `git ls-files -- docs/specs | wc -l` → 212

## Checklist

- [ ] Tests added or updated for this change
- [ ] `docs/specs/` updated if this changes behavior covered by a spec
- [ ] `CONTRIBUTING.md` conventions followed (no inline comments, no silent `except`)

Spec: <SP-ID, or remove this line if no spec applies>
