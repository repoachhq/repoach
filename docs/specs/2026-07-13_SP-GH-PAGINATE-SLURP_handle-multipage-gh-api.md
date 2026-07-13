---
id: SP-GH-PAGINATE-SLURP
title: Parse multi-page gh api output with --slurp and surface parse failures loudly
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

# Parse multi-page gh api output with --slurp and surface parse failures loudly

## Intent

`gh api --paginate` on an array endpoint emits one JSON array per page
concatenated (`[...][...]`), which `json.loads` cannot parse past the
first page. On any PR long enough to paginate, all three array-fetch
callers degrade silently to empty/None — disabling the anti-repeat-
hallucination machinery and stacking duplicate archive comments. Parse
the full multi-page output and stop degrading silently.

## Context

Audit 2026-07-13 finding M7. `src/ferova/review/gh_client.py`, three
`gh api --paginate` array callers:

- `list_review_comments` (lines 416-436): `--paginate` on
  `pulls/{pr}/comments`; `json.loads(res.stdout)` at 426; on
  `JSONDecodeError` logs and returns `[]` (427-433). >1 page →
  concatenated arrays → decode fails → `[]`, so the anti-repeat /
  hallucination-guard machinery that reads existing inline comments
  sees none.
- `find_archive_comment` (lines 496-525): `--paginate` on
  `issues/{pr}/comments`; decode failure returns `None` (513-519), so a
  long PR is treated as having no archive comment and a duplicate is
  posted.
- `fetch_archive_comment_with_status` (lines 596-625): same
  `--paginate` on `issues/{pr}/comments`; decode failure returns
  `ArchiveFetch(body=None, api_error=...)` — here the failure IS
  surfaced as `api_error`, but still fails to parse a legitimate
  multi-page payload.

`gh api --slurp` wraps the per-page arrays into a single JSON array of
arrays (or `--paginate` combined with `jq -s`); the correct fix reads
the merged list. These feed the review team's comment machinery on
every PR. Review-integrity change, not a merge-path change.

## Goals

- G1: multi-page `gh api --paginate` array output is parsed into the
  full merged list (via `--slurp` + flatten, or an equivalent correct
  parse), across all three callers.
- G2: a genuine parse failure (not merely "more than one page") is
  surfaced LOUDLY — not silently collapsed into `[]`/`None` — so a
  broken read cannot masquerade as "no comments".
- G3: `list_review_comments`, `find_archive_comment`, and
  `fetch_archive_comment_with_status` all return complete results on a
  paginated PR; no duplicate archive comment is posted on a long PR.

## Non-Goals

- NG1: no change to the single-object endpoints (`pr_head_sha`,
  `pr_view`) that already parse one object correctly.
- NG2: no change to write endpoints (`upsert_archive_comment` POST/
  PATCH bodies) beyond consuming the fixed `find_archive_comment`.
- NG3: no pagination-size tuning; correctness of the parse is the whole
  job.

## Assumptions

- A1: `gh api --slurp --paginate` (or `--paginate` with a jq flatten)
  yields a single parseable JSON document combining every page; a
  single-page response `--slurp`s to a one-element outer array that
  flattens back to the same list. Behavior is verified against a
  two-page fixture, not assumed live.
- A2: "surface loudly" means the existing structlog warning stays AND
  the failure is not indistinguishable from an empty result where the
  caller can fail closed (the archive path already models this via
  `api_error`; the review-comments path returns `[]` today — the fix
  keeps the loud log and, where the caller must fail closed, makes the
  distinction available).

## Interface

N/A (in-place fix). Return types of the three methods are unchanged;
the `gh api` argument list gains `--slurp` (and the decode step
flattens the outer array).

## Behavior

### Nominal

Single-page PR: each method returns the same list/None it does today.

### Edge cases

- Two-or-more-page PR: the merged list contains every page's elements
  (e.g. all inline review comments across pages); `find_archive_comment`
  locates a marker on page 2; no duplicate archive comment is posted.
- Empty result (no comments at all): `--slurp` yields `[]` (or `[[]]`
  flattening to `[]`); methods return `[]`/`None` as today.

### Failure scenarios

- Malformed/truncated `gh` output that is not valid JSON even after
  `--slurp` → the parse failure is logged loudly and, for the archive
  fetch, reported via `api_error`; callers fail closed rather than
  treating it as "no comments". Fail closed on unparseable reads.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `gh_client.py` (owned by an existing spec, the gh-client arc); no new
  cross-owner import.
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — given a two-page `--slurp`-shaped payload (a JSON
  array of two page-arrays), the parse helper flattens it into the full
  merged list; a malformed payload raises the loud path, not a silent
  `[]`.
- [ ] AC2 (INTEGRATION): drive `list_review_comments`,
  `find_archive_comment`, and `fetch_archive_comment_with_status`
  through a `GhCli` whose `_run` is a truthful boundary fake returning a
  realistic two-page `gh api --slurp` payload (an array-of-arrays with
  the archive marker on the second page). Assert: the full comment list
  is returned (both pages), the archive comment id is found, and no
  duplicate-post path is taken. A malformed-payload case asserts the
  loud failure surfaces (warning logged / `api_error` set), not a
  silent empty.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_gh_client.py::test_paginated_array_endpoints_parse_all_pages`
  and
  `tests/unit/test_gh_client.py::test_malformed_paginated_output_surfaces_loudly`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
