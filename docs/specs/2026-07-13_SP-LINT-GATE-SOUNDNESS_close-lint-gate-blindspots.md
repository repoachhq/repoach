---
id: SP-LINT-GATE-SOUNDNESS
title: Close the silent-except and inline-comment gate blind spots
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

# Close the silent-except and inline-comment gate blind spots

## Intent

The two golden-rule gates the factory trusts to catch swallowed
exceptions and per-line lint suppression each have soundness holes that
let the exact patterns they exist to forbid pass clean. Close them so a
green gate means what it claims.

## Context

Audit 2026-07-13 findings M13, M14, M26, plus two lint lows.

- M13 — `src/ferova/lint/no_silent_except.py:115-142`
  (`_call_looks_like_log` / `_body_contains_log_call`): a handler is
  cleared as "loud" when ANY call anywhere in its body has a terminal
  attribute in `_LOG_METHOD_NAMES`
  (`debug/info/warning/warn/error/exception/critical/log`) or is a bare
  call to such a name (`no_silent_except.py:64-66,131-133`). So
  `except Exception: cache.info(); pass` and
  `except: q.log(x); return None` pass while fully swallowing the
  exception — the "log" call is merely present, not actually logging
  the caught error.
- M14 — `no_silent_except.py:215-217,257`
  (`_allow_suppressed`): the `# allow-silent-except: <reason>` escape
  hatch MUST be an inline comment on the `except` line, but
  `no_inline_comments.py` forbids all inline comments (CLI default
  `--max 0`, `scripts/lint_no_inline_comments.py:49-54`). The two
  gates are mutually exclusive over `src/`, `tests/`, `scripts/`, and
  the promised one-line reason is never enforced (the regex
  `_ALLOW_RE`, `no_silent_except.py:58`, matches the directive but not
  a reason).
- M26 — `src/ferova/review/dev_runner.py:175,190`:
  `contextlib.suppress(OSError)` swallows exceptions with no log and is
  invisible to the gate, which walks only `ast.ExceptHandler` nodes
  (`no_silent_except.py:243-245`).
- Low — both scanners treat unparseable files as clean and return `[]`
  (`no_inline_comments.py:100-105`,
  `no_silent_except.py:229-239`): a file that fails to tokenise/parse
  is silently skipped rather than flagged.
- Low — `DEFAULT_ROOTS` are CWD-relative
  (`no_silent_except.py:45`, `no_inline_comments.py:43`) and missing
  roots are skipped silently (`iter_python_files` guards on
  `root.is_dir()`, `no_silent_except.py:278-284`,
  `no_inline_comments.py:138-141`). One listed root (`agents/`) does
  not exist in the tree at all, and a run from the wrong CWD scans
  nothing, prints `total=0`, and exits 0 — a vacuous pass.

Both scanners are pure library modules under `src/ferova/lint/`; the
CLI wrappers live at `scripts/lint_no_silent_except.py` and
`scripts/lint_no_inline_comments.py`; the pytest bindings are
`tests/unit/test_no_silent_except_gate.py` and
`tests/unit/test_no_inline_comments_gate.py`.

## Goals

- G1: a handler is cleared as loud ONLY when its body contains a log
  call that actually handles the caught exception — the call is a real
  logging emit (`_LOG_OBJECT_NAMES`-rooted) and either references the
  bound exception name or occurs as the direct trailing/adjacent
  logging of the handler, not merely any call whose terminal attribute
  happens to be `info`/`log`.
- G2: `contextlib.suppress(...)` calls that name broad exception types
  and carry no logging are reported as silent-except violations.
- G3: the escape-hatch is reconciled with the inline-comment gate — it
  no longer requires a forbidden inline comment — and its reason is
  enforced (an empty-reason suppression is rejected).
- G4: an unparseable/untokenisable file FAILS the gate (counts as a
  violation, non-zero exit) instead of being silently skipped.
- G5: a run that resolves zero existing roots (wrong CWD, or every
  configured root missing) FAILS with a non-zero exit instead of a
  vacuous `total=0` pass.

## Non-Goals

- NG1: no change to the ratchet baseline value `MAX_SILENT_EXCEPT`
  (`no_silent_except.py:47`) beyond what the newly-caught real
  violations force; if the tightened rules surface genuine debt, the
  fix is to clean the debt, not to raise the baseline.
- NG2: no new lint dimension (naming, complexity, ...) — soundness of
  the two existing gates only.
- NG3: no rewrite of the AST classification of silent trailers
  (`_classify_silent_trailer`) beyond adding the `contextlib.suppress`
  detection.

## Assumptions

- A1: `except <Type> as <name>:` handlers that log using the bound
  `<name>` are the honest pattern; a handler with no `as` binding that
  logs a real logger call in its body is also honest — the tightening
  targets calls that are NOT real logging emits and calls that ignore
  the exception entirely.
- A2: `contextlib.suppress(OSError)` for a single narrow, expected
  errno-class error is sometimes legitimate; the reconciled escape
  hatch (G3) is the sanctioned way to keep such a site.

## Interface

`src/ferova/lint/no_silent_except.py`:
- Tighten `_call_looks_like_log(call: ast.Call, *, exc_name: str | None) -> bool`
  — a call qualifies only when its function is rooted in
  `_LOG_OBJECT_NAMES` (a real logger object) AND (the handler binds no
  exception, or the call's arguments reference `exc_name`). A bare
  `q.log(...)`/`cache.info(...)` on a non-logger object no longer
  clears the handler.
- Add `_suppress_is_silent(node: ast.With) -> tuple[str, str] | None`
  — reports a `with contextlib.suppress(<Type>, ...):` block whose body
  emits no qualifying log call, mirroring the `ExceptHandler` path in
  `scan_file`.
- Replace the inline `# allow-silent-except` directive with a
  standalone-line directive recognised on the line ABOVE the `except`
  (or the `with contextlib.suppress`), of the exact form
  `# ferova: allow-silent-except reason="<non-empty>"`, so it is a
  whole-line comment the inline gate permits; a missing or empty
  `reason=` does NOT suppress.
- `scan_file` returns a sentinel `UnparseableFile` violation (new
  frozen dataclass or a `kind="unparseable"` `SilentExceptViolation`)
  instead of `[]` when read/parse fails.

`src/ferova/lint/no_inline_comments.py`:
- `scan_file` returns a `kind="unparseable"` violation instead of `[]`
  when tokenisation fails.
- Whole-line `# ferova: allow-silent-except ...` directives are not
  themselves reported (they sit on their own line; already allowed).

CLI wrappers (`scripts/lint_no_silent_except.py`,
`scripts/lint_no_inline_comments.py`):
- Resolve roots against the repository root (walk up from the module to
  the dir containing `pyproject.toml`), not CWD; when zero configured
  roots resolve to an existing directory, print a loud diagnostic and
  return exit code 2.

## Behavior

### Nominal

- `except ValueError as exc: _log.warning("x", error=str(exc)); pass`
  → cleared (real logger, references `exc`).
- `except ValueError: pass` → flagged (unchanged).

### Edge cases

- `except Exception: cache.info(); pass` → FLAGGED (`cache` is not a
  logger object; the call does not handle the exception).
- `except: q.log(x); return None` → FLAGGED (bare `log` on a
  non-logger name no longer clears).
- `with contextlib.suppress(Exception): risky()` → FLAGGED
  (`suppress` with no logging).
- A file with a syntax error under a scanned root → FLAGGED as
  `unparseable`; gate exits non-zero.
- A standalone-line `# ferova: allow-silent-except reason="narrow FS
  race, best-effort cleanup"` directly above a
  `with contextlib.suppress(OSError):` → suppressed, and the directive
  line does not trip the inline-comment gate.

### Failure scenarios

- Zero existing roots resolved → fail CLOSED: exit code 2 with a
  diagnostic naming the resolved root paths that did not exist, rather
  than exit 0 with `total=0`.
- Empty/missing `reason=` on a directive → the directive is ignored
  (fail CLOSED: the violation still reports).

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of the two
  `src/ferova/lint/*` modules and their two `scripts/` wrappers (all
  owned by existing lint-gate specs); introduces no new cross-owner
  import. `dev_runner.py` sites are either given the reconciled
  standalone directive with a reason or refactored to log — no
  ownership change there.
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place fix across two sibling lint modules).

## Acceptance Criteria

- [ ] AC1: unit — `_call_looks_like_log` clears a real logger call
  referencing the bound exception and REJECTS `cache.info()` /
  `q.log(x)` on non-logger names; `_suppress_is_silent` flags
  `contextlib.suppress(Exception)` with a no-log body and clears a
  suppress body that logs.
- [ ] AC2 (INTEGRATION): drive the two gate CLIs end-to-end via
  `subprocess`/`main(argv=...)` over a fixture tree written to a tmp
  dir containing (a) `except: log.info(); pass` where `log` is not a
  logger object, (b) a `with contextlib.suppress(Exception):` with no
  log, (c) a file with a syntax error, and (d) a run invoked from a
  wrong CWD / with all roots missing — each of (a)-(c) is reported and
  the process exits non-zero, and (d) exits code 2 with the
  missing-roots diagnostic (asserting the observed exit code and
  stdout, not a helper return value).
- [ ] AC3: promised tests —
  `tests/unit/test_no_silent_except_gate.py::test_non_logger_call_does_not_clear_handler`,
  `::test_contextlib_suppress_flagged`,
  `::test_allow_directive_requires_reason`,
  `::test_unparseable_file_fails`,
  `tests/unit/test_no_inline_comments_gate.py::test_unparseable_file_fails`,
  `::test_standalone_allow_directive_permitted`, and a CLI integration
  test `::test_missing_roots_exit_nonzero`.
- [ ] AC4: the reconciled escape hatch is applied at the two
  `dev_runner.py:175,190` sites (or they are refactored to log), and
  `python scripts/lint_no_silent_except.py` and
  `python scripts/lint_no_inline_comments.py` both exit 0 on the real
  tree from the repo root.
- [ ] AC5: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
