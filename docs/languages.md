# Language support

`SP-UNIVERSAL-GATES` drives this from one data table,
`LANGUAGE_TABLE` in `src/repoach/gates/languages.py`: a language is
detected by extension (or, for an extension-less file, by shebang),
and its files are judged by the verifiers its row names. This page
lists only the languages that have a verifier landed; a language can
be detected by the gate before it reaches this table — see
`CONTRIBUTING.md`'s "Adding a language" section for what landing one
takes.

| language | detected by | lint | test | verifier | status | maintainer |
| --- | --- | --- | --- | --- | --- | --- |
| Python | `.py` | ruff | pytest | `python_syntax`, `python_imports`, `python_house_lint`, `python_docstring` | stable | @jwfaye |
| TypeScript | `.ts`, `.tsx`, `.mts`, `.cts` | eslint | vitest | `tsc`, `eslint`, `ts_imports`, `jsdoc` | stable | @jwfaye |

Go and Rust are already detected (`LANGUAGE_TABLE` has rows for
`Language.GO` and `Language.RUST`, `.go` / `.rs` extensions) but
neither has a verifier yet, so neither is in the table above — adding
one is an open `good first spec` issue (`Language support: Go`,
`Language support: Rust`). A language proposed with no linter
available ships with `lint: none` and `status: experimental` rather
than waiting for one.
