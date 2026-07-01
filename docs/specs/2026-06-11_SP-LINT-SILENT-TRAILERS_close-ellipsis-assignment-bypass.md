# SP-LINT-SILENT-TRAILERS — close the `...` and silent-assignment bypasses in the silent-except gate

## Metadata

- **Status**: OPEN
- **Priority**: P2
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-11

## Why

The audit empirically confirmed three bypasses of the
SP-LINT-LOG-CATCH-ALL gate
(`src/ferova/lint/no_silent_except.py`,
`_classify_silent_trailer`):

- `except Exception: ...` — an `ast.Expr` holding `Ellipsis` is not
  `Pass`/`Continue`/`Return`, so the handler is not flagged. This is a
  one-character drop-in replacement for `pass`.
- `except Exception: result = None` — an assignment trailer is never
  classified, even when it assigns a silent sentinel.
- `return -1` (or any constant outside `_SILENT_TRAILING_CONSTANTS`)
  is not flagged — accepted as out of scope here (too many legitimate
  sentinel returns), documented as a known limit.

The gate's zero-tolerance promise (`MAX_SILENT_EXCEPT = 0`) is only as
strong as its trailer classifier.

## What

In `src/ferova/lint/no_silent_except.py`:

1. `_classify_silent_trailer` gains two cases:
   - `ast.Expr` whose value is `ast.Constant(Ellipsis)` →
     `("ellipsis", "...")`.
   - `ast.Assign` / `ast.AnnAssign` whose value is a silent constant —
     same value set as the `return` case: `None`/`False`/`0`/`""`/
     `b""` (via `_SILENT_TRAILING_CONSTANTS`) or an empty
     `List`/`Tuple`/`Dict`/`Set` literal → `("assign", "<target> = <value>")`.
     Assignments of *computed* values (calls, names, attribute reads)
     stay allowed — assigning a fallback result is a legitimate
     recovery pattern; assigning a bare silent sentinel as the last
     statement is not.
2. Update the module docstring's trailer taxonomy and the known-limits
   note (`return <non-silent constant>` remains unflagged by design).
3. Run the gate over `src/ tests/ scripts/`; if the new rules surface
   existing violations, fix each site properly (add a log emit before
   the trailer, or re-raise) — never with the
   `# allow-silent-except:` directive (it conflicts with the
   no-inline-comments gate inside the enforced roots; that conflict is
   tracked separately).

The `scripts/lint_no_silent_except.py` wrapper imports the scanner
from `src/ferova/lint/` — single source of truth, no wrapper
change needed.

## Files in scope

- `src/ferova/lint/no_silent_except.py`
- `tests/unit/test_no_silent_except_scanner.py`
- `tests/unit/test_no_silent_except_gate.py`
- Any source file the strengthened gate newly flags (fix in place)

## Out of scope

- Resolving the `# allow-silent-except:` ↔ no-inline-comments gate
  conflict.
- Tightening the spoofable log-recognition heuristic
  (`_call_looks_like_log` accepting any `*.log/info/...` terminal
  attribute).
- Flagging non-silent constant returns (`return -1`).

## Smoke scenario

### Setup

Write a tmp-path fixture module containing four handlers:
`except Exception: ...`, `except Exception: result = None`,
`except Exception: items = []`, and
`except Exception: result = compute_fallback()` (preceded by no log
call in each).

### Execute

Run `scan_file` on the fixture, then
`python scripts/lint_no_silent_except.py --summary` on the repo.

### Expected

The first three handlers are reported (kinds `ellipsis`, `assign`,
`assign`); the computed-fallback handler is not. The repo-wide summary
reports `total=0` after any in-repo fixes land.

## Definition of Done

- Ellipsis trailer flagged — `test_ellipsis_trailer_flagged`.
- Silent-constant and empty-container assignment trailers flagged
  (Assign and AnnAssign) — parametrised tests.
- Computed-value assignment trailer NOT flagged —
  `test_computed_assignment_allowed`.
- Handlers with a log call or re-raise before the new trailer kinds
  remain unflagged — regression tests.
- Repo-wide gate green (`total=0`) with the strengthened rules, all
  newly surfaced sites fixed at root.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(lint): flag ellipsis and silent-assignment except trailers`
2. `fix: log or re-raise at sites surfaced by the strengthened gate` (omit if none)
3. `test(lint): trailer-bypass regression cases`

## Risks

- **False positives on legitimate sentinel assignments**: mitigated by
  only flagging bare silent constants/empty literals, and only as the
  *last* statement of a handler with no preceding log/raise — the same
  contract the `return` case already uses.
