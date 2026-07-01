---
id: SP-SPEC-TEMPLATE
title: Spec format and frontmatter contract
version: 0.1
status: approved
author: agent
created: 2026-06-20
updated: 2026-06-20

owns:
  code: [docs/specs/_TEMPLATE.md]     # the canonical template artifact (a doc, not importable)
  resources: [format:spec-frontmatter]  # the frontmatter contract every governed spec + arch tool consumes

depends_on: []                        # true root of the spec-architecture arc
provides_to: []                       # AUTO-maintained (SP-ARCH-GRAPH parses it, SP-ARCH-EDGE-GATE enforces it)

constraints: {}
---

# SP-SPEC-TEMPLATE — spec format and frontmatter contract

## Intent
Define the canonical spec template and the machine-readable
`format:spec-frontmatter` contract that governs every NEW spec — the
single schema the architecture tooling (`SP-ARCH-GRAPH` deriver,
`SP-ARCH-EDGE-GATE`) resolves edges through. This is the root that makes
the dependency graph derivable instead of drawn by hand.

## Context
Owns `docs/specs/_TEMPLATE.md` and the frontmatter schema it encodes.
Every governed spec is a copy of the template with the guidance comments
removed. `SP-ARCH-GRAPH` parses this frontmatter; `SP-ARCH-EDGE-GATE`
enforces that a diff's couplings match the declared `depends_on`. Applies
to NEW specs only — legacy specs stay frozen frontier nodes, with a
voluntary promotion path (give a legacy spec an `owns`, pull it out of
the frontier) taken opportunistically when its zone is next touched.

## Goals
- G1: A canonical template file carrying the locked section structure
  (Intent … Open Questions) and the frontmatter block.
- G2: A frontmatter contract specifying: required keys (`id`, `title`,
  `version`, `status`, `owns`, `depends_on`); the `owns: {code, resources}`
  shape; the resource-key grammar; `depends_on` as a list of SP-IDs.
- G3: The governing conventions, each stated once: `id` == SP-ID;
  "complete = every section PRESENT, `N/A` allowed"; `depends_on` is the
  single canonical edge source and `## Architecture Impact` is its WHY;
  frontier is reported-not-enforced and proportionate; legacy promotion is
  voluntary.

## Non-Goals
- NG1: Does NOT implement parsing/derivation (that is `SP-ARCH-GRAPH`) or
  diff-time enforcement (that is `SP-ARCH-EDGE-GATE`).
- NG2: Does NOT retrofit or require frontmatter on legacy specs.
- NG3: Does NOT define numeric/domain constraints — `constraints:` is a
  per-spec, optional extension point.

## Assumptions
- A1: Specs carry YAML frontmatter delimited by `---` fences.
- A2: Specs live at `docs/specs/<date>_<SP-ID>_<slug>.md`; underscore-
  prefixed files (`_TEMPLATE.md`) are not specs.

## Interface
This is a *contract* component — its interface is a data schema, not a
function signature.

Frontmatter schema (`format:spec-frontmatter`):
- `id`: str — the SP-ID; globally unique; identical to the filename SP-ID,
  the branch, and any `depends_on` reference to this spec.
- `title`: str — human-readable name.
- `version`: str — bumped on revision. `status`: `draft|reviewed|approved`.
- `owns.code`: list[path] — owned version-controlled artifact paths
  (usually `src/...`; may be a governed doc like `_TEMPLATE.md`, on which
  the import-honesty tier never fires). Disjoint across specs.
- `owns.resources`: list[resource-key] | `N/A` — owned shared
  state/contracts. resource-key grammar:
  - `db:table:<name>` — enforced (tier 1b);
  - `queue:topic:<name>` — declarative (tier 2);
  - `format:<name>` — declarative (tier 2).
  Disjoint across specs.
- `depends_on`: list[SP-ID] — the canonical architecture edges this
  component needs, by import (code) or shared resource.
- `provides_to`: list[SP-ID] — AUTO-maintained reverse edges; authored `[]`.
- `constraints`: mapping — optional, domain-specific.

## Behavior

### Nominal
A conformant spec: valid frontmatter with all required keys; every body
section present (filled or `N/A`); `Open Questions` empty before
`status: approved`.

### Edge cases
- a section that does not apply ⇒ `N/A` + one-word reason (not a violation).
- `owns.resources: N/A` ⇒ a code-only component.
- a legacy spec with no frontmatter ⇒ frontier node, tolerated.

### Failure scenarios
- a required frontmatter key missing / malformed ⇒ rejected by
  `SP-ARCH-GRAPH` (`MalformedFrontmatterError`).
- the same path/resource owned by two specs ⇒ disjointness violation
  (`OwnershipConflictError`).

## Architecture Impact
- **Root** of the spec-architecture arc; introduces the template artifact
  and the `format:spec-frontmatter` resource.
- `depends_on: []` — depends on no governed component.
- `provides_to`: `SP-ARCH-GRAPH` (parses the format) and
  `SP-ARCH-EDGE-GATE` (enforces it) both depend on this resource.
- New coupling / cycles / shared state: none.

## Diagram
N/A — contract component; the only flow is "every governed spec is an
instance of this format", which the system graph already expresses.

## Acceptance Criteria
- [ ] AC1: `docs/specs/_TEMPLATE.md` exists and carries every locked
  section (Intent, Context, Goals, Non-Goals, Assumptions, Interface,
  Behavior, Architecture Impact, Diagram, Acceptance Criteria, Open
  Questions) and the frontmatter block.
- [ ] AC2: a test asserts the template frontmatter declares the required
  keys (`id`, `title`, `version`, `status`, `owns`, `depends_on`) and the
  `owns: {code, resources}` shape.
- [ ] AC3: the resource-key grammar (`db:table:`, `queue:topic:`,
  `format:`) and the `id` == SP-ID convention are documented in the
  template's guidance block.
- [ ] AC4: this spec and `SP-ARCH-GRAPH` round-trip through the future
  `SP-ARCH-GRAPH` parser without a `MalformedFrontmatterError` (cross-
  checked by that component's suite).

## Open Questions
- None. (Resolved while drafting: `owns.code` may hold a governed doc
  path — the import tier simply never fires on a non-importable file;
  Interface of a contract component is its data schema, not a signature.)
