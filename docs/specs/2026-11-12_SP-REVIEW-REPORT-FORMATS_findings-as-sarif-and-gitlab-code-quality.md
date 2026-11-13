---
id: SP-REVIEW-REPORT-FORMATS
title: The findings of a review are written as SARIF and as a GitLab code-quality report, from the ledger
version: 0.1
status: draft
author: agent
created: 2026-11-12
updated: 2026-11-12

owns:
  code: [src/repoach/review/report_formats.py, tests/unit/test_report_formats.py, tests/fixtures/report_formats/sarif-2.1.0-minimal.json]
  resources: [format:repoach-sarif, format:gitlab-codequality-json]

depends_on: [SP-FINDING-MODEL, SP-FINDINGS-BRIDGE-DOCFIX, SP-CLI-STDOUT-ONE-JSON, SP-UNIVERSAL-CI-ENTRY, SP-UNIVERSAL-PROJECT-CONFIG, SP-ORDER-INDEPENDENT-DEBT-IDS, SP-OWNER-SETTLES-A-FINDING, SP-REFUTER-JUDGES-WHAT-IT-CAN]
provides_to: []

constraints:
  sarif_version: "2.1.0"
---

# The findings of a review are written as SARIF and as a GitLab code-quality report

## Intent

A review's findings leave repoach as one JSON object of repoach's own
shape, an archive comment, and inline threads. GitLab shows a merge
request's code-quality report in its widget and GitHub ingests SARIF
into code scanning; neither reads repoach's JSON. After this spec
`review pr`, `review gate` and `ci review` write the ledger's findings
at the head as SARIF 2.1.0 or as a GitLab code-quality report, from the
same rows the report is built from, with each finding's lifecycle
status carried as a property so a refuted or owner-resolved finding is
never shown as live.

## Context

Measured on `origin/develop` `8fabb19` (2026-11-12).

| # | Fact | Where |
|---|---|---|
| F1 | `review pr` prints one JSON object on stdout; logs go to stderr | SP-CLI-STDOUT-ONE-JSON; `src/repoach/cli/review_cmds.py:262` |
| F2 | The report is rendered from the findings ledger and the re-verified facts; every finding that can move the verdict is listed with its judge | `src/repoach/review/report.py`, module docstring |
| F3 | A finding has `claim_type`, `severity` (`blocker`, `major`, `minor`, `nit`), a status from `FindingStatus`, a file, a line, a fingerprint, a ledger id | `src/repoach/review/findings.py` (`Finding`, `Severity`, `FindingStatus`) |
| F4 | `repoach init --ci gitlab` writes `.repoach/ci/gitlab-review.yml`; the job runs `repoach ci review`, advisory, JSON on stdout | README "Run repoach in CI"; `src/repoach/ci_entry/snippets.py` |
| F5 | GitLab's code-quality artefact is a JSON array of `{description, check_name, fingerprint, severity, location: {path, lines: {begin}}}` with severities `info, minor, major, critical, blocker`, declared as `artifacts: reports: codequality: <file>` | GitLab docs, "Code Quality" report format (read 2026-11-12; not pinned by a test, the fixture pins repoach's output) |
| F6 | open-code-review writes SARIF 2.1.0 (`$schema` `https://json.schemastore.org/sarif-2.1.0.json`), one rule per category, one result per comment, level from severity | `R/cmd/opencodereview/sarif.go:27-28,131,155,174` |

## Decisions (author, within the owner's brief)

- D1. One module, two renderers over the same input: the findings of
  the head as the report reads them (F2), never the reviewer outcomes.
  `to_sarif(findings, head, tool_version)` and
  `to_gitlab_codequality(findings)`.
- D2. SARIF: `version 2.1.0`, one run, `tool.driver.name = "repoach"`,
  one rule per `(role, claim_type)` with the role's description, one
  result per finding whose status is not `refuted`, `owner_resolved`,
  `resolved` or `resolved_at_head`; `level`: blocker and major →
  `error`, minor → `warning`, nit → `note`; `locations[0]` the file and
  the derived line; `fingerprints["repoach/v1"]` the finding's
  fingerprint; `properties`: `finding_id`, `status`, `judge`
  (`mechanical:<claim_type>`, `refuter`, `deferred:<reason>`,
  `unverified:<reason>`), `head_sha`. A `proposed` finding the refuter
  deferred is included with `status: proposed` and `level: note`, so a
  consumer can filter, and nothing is silently dropped.
- D3. GitLab code quality: the same selection; `check_name` =
  `repoach/<role>/<claim_type>`; `severity`: blocker → `blocker`, major
  → `major`, minor → `minor`, nit → `info`; `fingerprint` the finding's
  fingerprint (stable across runs, SP-ORDER-INDEPENDENT-DEBT-IDS'
  property, so GitLab's widget shows new and fixed findings between
  pipelines); `description` the body prefixed with the status when it is
  not `verified` or `open` (`[proposed] …`).
- D4. CLI: `--format json|sarif|gitlab-codequality` (default `json`)
  and `--output <path>` on `review pr`, `review gate` and `ci review`.
  With `--output`, stdout keeps the one JSON object and the file holds
  the document; without it, stdout holds the chosen document alone
  (one document on stdout, F1's rule kept). `ci review` on GitLab adds
  `--output gl-code-quality-report.json` to the generated job and the
  `artifacts: reports: codequality:` key when `init --ci gitlab` is run
  with `--code-quality` (O1).
- D5. The SARIF document is validated in tests against a committed
  minimal schema excerpt (`tests/fixtures/report_formats/`), not fetched;
  the GitLab document against the field list of F5 pinned in the test.

## Goals

- G1: `to_sarif` and `to_gitlab_codequality` render from the head's findings (D1–D3); refuted and settled findings are absent, deferred ones carry their status.
- G2: `--format` and `--output` exist on the three commands, stdout holds exactly one document (D4).
- G3: `init --ci gitlab --code-quality` writes the artefact key and the output flag (D4, O1).
- G4: fingerprints in both formats equal the ledger's (D3).

## Non-Goals

- NG1: no upload to GitHub code scanning by repoach; the user's workflow uploads the file.
- NG2: no change to the archive comment or the JSON.
- NG3: no GitLab SAST format (a security-only report); code quality carries every role.

## Assumptions

- A1: GitLab's widget accepts `fingerprint` values of any printable string of reasonable length; repoach's are 12-hex.

## Interface

```python
# repoach.review.report_formats
SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
LIVE_STATUSES: frozenset[FindingStatus]      # everything but refuted, owner_resolved, resolved, resolved_at_head
def to_sarif(findings: Sequence[Finding], *, head_sha: str, tool_version: str) -> dict: ...
def to_gitlab_codequality(findings: Sequence[Finding]) -> list[dict]: ...
def write_format(doc: dict | list, fmt: str, output: Path | None) -> None: ...
```

CLI: `repoach review pr <N> [--format json|sarif|gitlab-codequality] [--output PATH]`, the same on `review gate` and `ci review`; `repoach init --ci gitlab --code-quality`.

## Behavior

### Nominal
- `review pr 42 --format gitlab-codequality --output gl-code-quality-report.json`: stdout the usual JSON; the file an array with one entry per live finding.

### Edge cases
- Zero live findings: SARIF with `results: []`, GitLab `[]`; exit codes unchanged.
- A finding with no line (a whole-file claim): SARIF `region` omitted, GitLab `lines.begin = 1`.

### Failure scenarios
- `--output` to an unwritable path: rc 2 naming the path, after the review ran and its JSON was printed (the review is not lost to a report error).

## Architecture Impact
- Adds dependency: SP-REVIEW-REPORT-FORMATS -> SP-FINDING-MODEL, SP-FINDINGS-BRIDGE-DOCFIX, SP-ORDER-INDEPENDENT-DEBT-IDS (the finding rows, their statuses, their stable fingerprints)
- Adds dependency: SP-REVIEW-REPORT-FORMATS -> SP-OWNER-SETTLES-A-FINDING, SP-REFUTER-JUDGES-WHAT-IT-CAN (`owner_resolved` and the deferral reasons carried as properties)
- Adds dependency: SP-REVIEW-REPORT-FORMATS -> SP-CLI-STDOUT-ONE-JSON, SP-UNIVERSAL-CI-ENTRY, SP-UNIVERSAL-PROJECT-CONFIG (the flags, the generated job)
- `report_formats.py` imports `findings` only; the three CLI modules import it.

## Diagram
N/A: two pure renderers over the report's input.

## Acceptance Criteria

- [ ] AC1: `pytest -q tests/unit/test_report_formats.py` → green.

| Test | Pins | Mutation that turns it red |
|---|---|---|
| `test_sarif_shape_and_version` | `version == "2.1.0"`, schema URL, one run, driver `repoach` | the version literal |
| `test_refuted_and_settled_findings_are_absent` | 6 findings, 2 refuted, 1 owner_resolved → 3 results | `LIVE_STATUSES` includes `refuted` |
| `test_deferred_finding_is_a_note_with_its_status` | `proposed` → `level note`, `properties.status proposed`, `judge deferred:<reason>` | deferred findings dropped |
| `test_level_from_severity` | blocker/major → error, minor → warning, nit → note | major → warning |
| `test_fingerprints_equal_the_ledger` | `fingerprints["repoach/v1"]` and GitLab `fingerprint` equal `finding.fingerprint` | a hash of the body is used |
| `test_gitlab_fields_and_severities` | the five keys of F5; nit → info, blocker → blocker | `check_name` missing |
| `test_stdout_holds_one_document` | `--format sarif` without `--output`: stdout parses as one JSON value and it is SARIF | the review JSON is printed too |
| `test_output_file_keeps_stdout_json` | with `--output`, stdout is the review JSON | stdout is empty |
| `test_unwritable_output_is_rc_2_after_the_json` | the JSON was printed, then rc 2 naming the path | the exception escapes as a traceback |

- [ ] AC2: `pytest -q tests/unit/test_init_ci.py -k code_quality` → green: `init --ci gitlab --code-quality --print` carries `artifacts:\n    reports:\n      codequality: gl-code-quality-report.json` and the `--output` flag; without the option, neither. Mutation: always write the key.
- [ ] AC3: `repoach arch check --base develop` → ok.

## Success conditions
- C1: a recorded Yoob fixture rendered as GitLab code quality holds one entry per live finding and the entries' fingerprints equal the `pr_findings` rows'.
- C2: the SARIF of the same fixture validates against the committed schema excerpt.

## Owner decisions needed
- O1: `--code-quality` opt-in on `init --ci gitlab` (proposed) or on by default for GitLab jobs.
- O2: whether `review gate` should also render formats (proposed: yes, the gate reads the same ledger) or `review pr` and `ci review` only.

## Open Questions
- Q1: O1 and O2.
