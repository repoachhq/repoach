# The labelled corpus

`repoach bench` measures how often a repoach finding is right
(`SP-PRECISION-MEASUREMENT`). A **corpus** is a directory of case files;
its name is the directory name (`planted`, `yoob`). Each case says where
its diff comes from, which defects were planted in it, and how a human
labelled the findings a run produced.

```bash
repoach bench run bench/corpus/planted --provider claude_code   # review every case, write bench/results/<date>_planted_claude_code.json
repoach bench label bench/results/<file>.json                   # add a stub per new finding to its case file
repoach bench report bench/results/<file>.json                  # the two tables; --markdown, --readme-line, --update-readme README.md
repoach digest --since 7d                                       # what the gate refused this week, from the ledger
```

`bench run --dry-run` validates the corpus and builds every case without
calling a model. `bench score` is another name for `bench report`.

## A case file

```yaml
case: 01-security-sql-injection        # unique in the corpus: [a-z0-9][a-z0-9._-]*
description: store.find_by_name interpolates the search fragment into the SQL text
private: false                         # true: label stubs carry no hint (the code is not public)
source:
  kind: local                          # local | forge | ledger
  fixture: ../../fixtures/sample       # local: the base tree, relative to this file
  patch: ../../fixtures/patches/01-security-sql-injection.patch
planted:                               # the defects the patch introduces (empty for a real PR)
- file: src/inventory/store.py
  lines: [58, 60]                      # inclusive range a finding must overlap
  kind: security                       # security | logic | test_gap | docstring | convention
  severity: blocker                    # blocker | major | minor | nit
  note: what the defect is
findings:                              # one entry per finding of a run, keyed by fingerprint
- fingerprint: 3f2a9c01b7d4
  label: true                          # true | false | unsure | null (not labelled yet)
  labeller: owner                      # a person; a model never labels (NG2)
  labelled_on: 2026-10-21
  hint: 'sentinel blocker src/inventory/store.py:60: SQL built from …'
```

The other sources:

```yaml
source:                                # a PR / MR of the repository `bench run` runs in
  kind: forge
  repo: https://gitlab.com/owner/project
  pr: 296
  head_sha: d62ae6f5c45332e9ef62f7b6abbe06e7c3590ddc   # optional; a forge head that moved skips the case
---
source:                                # findings already in a ledger, scored without a new review
  kind: ledger
  repo: https://gitlab.com/owner/project
  pr: 296
  head_sha: d62ae6f5c45332e9ef62f7b6abbe06e7c3590ddc   # bench run --ledger-db <path> names the ledger
```

`.toml` case files are read the same way; `bench label` writes YAML only.

## Labelling: one minute per finding

`repoach bench label <result>` appends, to each case file, one stub per
finding the corpus does not list yet:

```yaml
- fingerprint: 8d1e44a0c2f9
  label: null
  labeller: null
  labelled_on: null
  hint: 'architect major src/inventory/pricing.py:20: apply_discount now returns …'
```

Read the hint (or, for a private case, the claim in the result file under
`cases[].findings[]` with the same fingerprint), then set `label` to
`true` (the finding is a real defect), `false` (it is not) or `unsure`,
and fill `labeller` and `labelled_on`. `--interactive` asks for each
label in turn (`t` / `f` / `u`, `s` skips, `q` stops) and writes the
three fields itself. Labels are read when `bench report` runs, so
labelling never needs a new run.

A **fingerprint** is a hash of the reviewer, the claim type, the file
and the line window (`line_start // 5`), never of the claim's words: two
runs word the same finding differently. `bench report` prints the share
of labelled fingerprints a run found again; under 70 % `bench run` exits
4 `labels stale` (assumption A1).

## Scoring

| label | not refuted | refuted |
|---|---|---|
| `true` | true positive | wrong refutation |
| `false` | false positive | correct refutation |
| `unsure` | counted on its own line, excluded from every ratio | |
| `null` | unlabelled, excluded, listed by `bench label` | |

- precision = TP / (TP + FP)
- refuter precision = correct refutations / (correct + wrong refutations)
- recall = planted defects found / planted defects; a defect is found
  when a non-refuted finding labelled `true` cites its file on an
  overlapping line range. Two findings on one defect count it once.

Every ratio is printed with its numerator and denominator, and
`cannot say` when the denominator is 0. Per reviewer, a defect counts as
found by the reviewers whose findings found it; per severity, TP and FP
are keyed by the finding's severity and recall by the planted defect's.
`report` also prints the planted defects *located* before labelling (a
non-refuted finding overlaps them): an upper bound on recall, not recall.

The published line (`report --readme-line`) is refused on a partial run
and below 100 labelled findings (`corpus too small: N < 100`, exit 2).

## The corpora

| Corpus | Cases | Source |
|---|---|---|
| `planted` | 10 | `bench/fixtures/sample` (a small Python + TypeScript project) and one patch per defect under `bench/fixtures/patches/`: two each of security, logic, test gap, docstring and convention. |

The Yoob merge requests reviewed so far become `ledger` (or `forge`)
cases with `private: true`, labelled by the owner; their diffs and
claims never enter this repository (NG3), only fingerprints and labels.
