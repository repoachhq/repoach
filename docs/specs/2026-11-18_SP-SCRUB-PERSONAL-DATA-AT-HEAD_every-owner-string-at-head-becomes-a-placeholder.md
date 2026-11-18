---
id: SP-SCRUB-PERSONAL-DATA-AT-HEAD
title: Every maintainer path, login, host, address and handle at HEAD becomes a placeholder or a class under one redaction rule; the systemd units are rendered with an absolute install path; the live units are untouched
version: 0.4
status: reviewed
author: agent
created: 2026-11-18
updated: 2026-11-18

owns:
  code: [deploy/systemd/repoach-chainpilot.service, deploy/systemd/repoach-llm-proxy.service, deploy/systemd/repoach-nim-health.service, deploy/systemd/repoach-chainpilot.timer, deploy/systemd/repoach-nim-health.timer, deploy/systemd/README.md, scripts/install_user_units.sh, tests/unit/test_user_units_take_an_install_path.py, tests/unit/test_ci_portability.py, docs/security_posture.md]
  resources: [format:redaction-rule]

depends_on: [SP-UNIVERSAL-FORGE, SP-PRECISION-MEASUREMENT, SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ, SP-OWNER-SETTLES-A-FINDING, SP-FINDING-THREAD-IDS, SP-CI-JOBS-NO-OP-WHEN-NOT-AFFECTED, SP-CI-SUPPLY-CHAIN-HARDEN, SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF, SP-AUTOMERGE-EVENT-DRIVEN, SP-CONTRIBUTING, SP-GITLAB-NOTE-RECEIVER, SP-MEMORY-BACKEND-LIVENESS, SP-CI-LEDGER-PERSISTS, SP-REVIEW-DELTA-FIRST, SP-REVIEW-SETTLED-STAYS-SETTLED, SP-UNIVERSAL-REPO, SP-ANONYMISED-GAP-SIGNAL, SP-REVIEW-MEMORY-LOOP, SP-CONVENTIONS-GATE, SP-PIN-TOOLCHAIN, SP-CLAUDE-CODE-FIRST-CLASS, SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB, SP-REFUTER-JUDGES-WHAT-IT-CAN, SP-GATE-NAMES-FORMAT-SKEW, SP-RELEASE-PR-CI-RUNS-FOR-THE-BOT-ACTOR, SP-SKIPPED-REQUIRED-CHECK-IS-NOT-GREEN]
provides_to: []

constraints:
  documentation_ipv4_block: 203.0.113.0/24
  documentation_domain: example.com
  render_token: "@REPOACH_HOME@"
  specs_bumped: 20
---

# Every owner string at HEAD becomes a placeholder

Amendment (0.2, 2026-11-18): the verifier's NO-GO on 0.1 re-measured.
`${REPOACH_HOME}` does not expand in `WorkingDirectory=` (measured,
D3), so the units are rendered by the install script; the recorder
redaction moved to SP-RECORDER-REDACTS-BY-CLASS; the inventory of D2
and the bump list of D7 completed (20 specs); the actor variable is
set first (O0); the allow rule is named `denylist` like child 3's; the
conditions recounted (C1 four lines, not one).

Amendment (0.3, 2026-11-18): the verifier's NO-GO on 0.2. C1's two
greps are scoped so that this family's own specs, which describe the
patterns, do not fail them (measured on the branch tree: four lines and
zero), and the `<layout>` grep is gone, since D1 prescribes `<layout>`
as the replacement; the D1 row for `name` follows
SP-RECORDER-REDACTS-BY-CLASS 0.2 (dropped in a user object only).

Amendment (0.4, 2026-11-18): the recorded `id` keys are kept, user
objects included — `gitlab.py:728` reads a user's `id` and the
approvals fixture pins two of them (SP-RECORDER-REDACTS-BY-CLASS 0.3
F1c); D1 and D2 drop only the user `name`.

## Intent

Child 1 of SP-REPO-CARRIES-NO-PERSONAL-DATA. It rewrites, at HEAD and
once, every occurrence its parent measured (F1–F9), under one written
redaction rule that every future fixture, doc and spec follows; it
makes the three user units install anywhere by rendering them with an
absolute path; and it names the owner's steps (the actor variable, the
commit identity, the migration of the live units) as steps, not
conditions.

## Context

The parent's F1–F9, F11, F15–F16, F18 and F24 are the inventory; this
spec cites them by number. What matters for the edits:

- Three specs use a grep that spells the forbidden strings as their
  oracle (F15). Those oracles are replaced by the class-based lint of
  SP-NO-PERSONAL-DATA-GATE, which lands after this spec; until then the
  oracle line reads `scripts/lint_no_personal_data.py --tree <path>`
  and is judged when that child lands (D6).
- The workflows' fallback `'["<gh-login>"]'` is a default (F8, 12
  lines); the guard test `tests/unit/test_ci_portability.py:72` checks
  only a former handle's literal, spelled, so it passes today and must
  be made generic. With the fallback empty and the variable unset, no
  pull request runs any job (F24), so the variable is set first (O0).
- The units' `%h/<layout>/repoach` (F7) is the maintainer's layout;
  systemd's `%h` is the invoking user's home, so the path is not
  personal by itself, but the layout under it is one maintainer's and
  `deploy/systemd/README.md:4-11` says so ("adjust … if either
  differs"). Measured 2026-11-18 on systemd 255 with
  `systemd-analyze --user verify`: the three units rewritten with
  `Environment=REPOACH_HOME=<abs>` and `${REPOACH_HOME}` in
  `WorkingDirectory=` are refused, "`WorkingDirectory= path is not
  absolute: ${REPOACH_HOME}`", exit 1 — `WorkingDirectory=` takes
  `%`-specifiers, not environment expansion; the same units rewritten
  with the absolute path in place verify with empty output, exit 0; the
  same with an absolute path whose `.venv/bin/repoach` does not exist
  print "`Command … is not executable`", exit 1. The design follows the
  measurement: render, do not expand (D3).
- The recording layer keeps `login`, `name`, `username` and a user
  object's `id` (F16): a re-recorded fixture would carry the handle
  again. The rule has to be applied where fixtures are written; that is
  SP-RECORDER-REDACTS-BY-CLASS, which lands after this spec and
  depends on the fixture shape this spec writes by hand (D2).

## Decisions (2026-11-18)

- D1. **One redaction rule (`format:redaction-rule`), written in
  `docs/security_posture.md` as a new section "Personal data in
  fixtures and docs", one table.** Every future fixture, bench result,
  doc and spec obeys it; the lint of SP-NO-PERSONAL-DATA-GATE enforces
  the classes it can detect and the deny-list the rest. The rule:

  | Class | Replacement | Note |
  |---|---|---|
  | absolute home path (`/home/<x>/…`, `/Users/<x>/…`) | `~/…` in prose; `<repo>/…` when the path is a checkout; `/path/to/…` in a recorded JSON value | never `%h/<layout>` either |
  | a maintainer's directory layout under the home | `<layout>`; in a unit, the render token `@REPOACH_HOME@` (D3) | |
  | system login, service account | `<user>`; in a recorded JSON value, the class word (`maintainer`, `author`, `bot`, `agent`, `service`) | SP-REVIEW-SETTLED-STAYS-SETTLED 0.7 rule, generalised |
  | forge handle in a fixture or test | the class word with a stable suffix when several are needed: `maintainer`, `author`, `author-2`, `bot` | GitHub `login`, GitLab `username` alike |
  | real name in a recorded user object | dropped: the `name` key is removed from the user object | `login`/`username` are read by the forges and stay as class words (`forge/recording.py:32-130`); a user object's `name` is read by nothing and is dropped, while `name` elsewhere (check runs, jobs) is read and kept; every `id` is kept — a user's is read by `gitlab.py:728` and an id is an opaque identifier in none of the five classes (SP-RECORDER-REDACTS-BY-CLASS D1, F1b, F1c); `node_id`-named keys are already dropped (`:138`); a URL value keeps its shape with a placeholder host and namespace (D1b) |
  | hostname (short or FQDN) | `<host>`; in a URL, `example.com` or a subdomain of it | RFC 2606 |
  | public IPv4 | `203.0.113.<n>` (RFC 5737 TEST-NET-3) | `192.0.2.<n>` and `198.51.100.<n>` are also accepted by the gate |
  | public IPv6 | `2001:db8::<n>` (RFC 3849) | |
  | mailbox | `<user>@example.com`; a forge noreply stays as is | |
  | a domain of the owner's other product | `example.com`, or `product.example.com` when the sentence needs a product | the product's name alone is not personal data (parent D6) |

- D2. **The scrub list, file by file, is the parent's F1–F9**, with the
  replacement each gets:
  - `bench/results/2026-10-21_planted_claude_code.json:4`: `"corpus_root": "bench/corpus/planted"` — a repository-relative path. `repoach bench score` resolves `Path(result.corpus_root)` relative to the working directory (`src/repoach/cli/bench_cmds.py:152,237`), so a relative value works from the checkout root and the file documents it in a sibling `README.md` line.
  - `docs/plans/2026-10-18_yoob-production-plan.md`: lines 3-4 and 27 (home path, login, layout) → `<repo>` and `<user>`; lines 38, 48, 52 (the handle) → "the maintainer"; lines 96-98, 100, 112, 122-123 (`<host-a>`) and 97 (`<host-b>`) → `<host-a>`, `<host-b>` (the plan compares two machines, so two placeholders); lines 100, 112 (the service account) → `<service user>`.
  - the six specs of F1–F2, the eight of F5 (`…SP-REFUTER-JUDGES-WHAT-IT-CAN_*.md:176` included) and the seven of F8's spec bodies (13 lines: `…SP-CI-SUPPLY-CHAIN-HARDEN_*.md:47,69,97,145`, `…SP-AUTOMERGE-EVENT-DRIVEN_*.md:151`, `…SP-FINDING-THREAD-IDS_*.md:48`, `…SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF_*.md:62,390`, `…SP-GATE-NAMES-FORMAT-SKEW_*.md:56`, `…SP-RELEASE-PR-CI-RUNS-FOR-THE-BOT-ACTOR_*.md:43,47,57`, `…SP-SKIPPED-REQUIRED-CHECK-IS-NOT-GREEN_*.md:48`): the spelled strings → the D1 placeholders ("the maintainer's handle" where a sentence names the actor); the grep oracles of F15 → `scripts/lint_no_personal_data.py --tree <fixture path>` → `personal-data: ok` (D6). Each edited spec gets a version bump and an amendment paragraph of two sentences naming this spec (D7).
  - `…SP-GITLAB-NOTE-RECEIVER_*.md:734,740-758`: AC3's grep → the lint; the example section keeps its structure with `<host>`, `203.0.113.10`, `example.com` and `product.example.com` in place of the measured values, and its title says "an operator's host, values replaced by placeholders".
  - `tests/fixtures/generated_artefacts/yoob_322/embeddings.diff.template:16`: `"produced_on": "maintainer"`; `corpus.diff:9-10,18-19` and `user_guide.diff:9-10,18-19,27-28`: the two product hosts → `product.example.com` and `studio.product.example.com`; `tests/integration/test_generated_artefacts_replay_yoob_322.py:118,124,125`: the same two strings. The test is green because it asserts the fixture's strings, not the live product's (`:118-125` build the expected tree from literals).
  - `tests/fixtures/forge/github/*.json` (7), `tests/fixtures/forge/gitlab/is_maintainer*.json` (4), `tests/fixtures/forge/gate/pr202_capture.json`: `login`/`username` → `maintainer`; the `name` key of the recorded `author` object removed, its `id` kept (F9; SP-RECORDER-REDACTS-BY-CLASS F1c); the URL paths that embed the handle (`collaborators/<handle>/permission`, `members/all?query=<handle>`) → `maintainer`. `tests/unit/test_forge_conformance.py:267,279,595-608`, `tests/unit/test_review_gate_forge_parity.py:52` and the fixtures' `args` move together, since `ReplaySpawn` fails on the first argv that differs (`forge/recording.py:285-300`). This is the shape SP-RECORDER-REDACTS-BY-CLASS makes the recorder write.
  - the twelve test modules and fakes of F8: the handle → `maintainer` (a module constant `MAINTAINER = "maintainer"` where it repeats ≥ 3 times: `test_owner_commands.py`, `test_finding_posts.py`, `github_concurrency_sim.py`, `merge_wait_fakes.py`); `tests/unit/test_ci_portability.py:72`: the spelled former-handle assertion becomes generic — no workflow contains `fromJSON('["` (an inline literal actor list of any content).
  - `.github/workflows/{ci,auto-review,merge-on-ci,shellcheck}.yml` (12 lines) and the two recorded workflow fixtures `tests/fixtures/merge_wait/*.yml` (5 lines): `vars.REPOACH_ALLOWED_ACTORS || '["<gh-login>"]'` → `vars.REPOACH_ALLOWED_ACTORS || '[]'`. Fail closed: with the variable unset, no pull request from anyone runs on the self-hosted runner, which is why O0 precedes this pull request. `docs/getting_started.md:323-326` and `docs/security_posture.md:47-69` (the prose and the table's "Default when unset" column) say the variable is required: "the workflows run no pull-request job until it is set". `tests/unit/test_ci_portability.py` gains `test_actor_allowlist_has_no_login_fallback`: every `fromJSON(vars.REPOACH_ALLOWED_ACTORS || …)` fallback is the literal `'[]'`.
  - `SECURITY.md:31`, `docs/languages.md:14-15`, `LICENSE:3`, `pyproject.toml` `authors`, the 81 `author:` frontmatter lines: kept (parent D3). Two of them carry the handle and are covered by one allow entry each of SP-NO-PERSONAL-DATA-GATE, rule `denylist`, reason `attribution`; the other three carry the real name or the git `user.name`, which are in no class and not in the recommended deny-list, so they need no entry.
- D3. **The units carry a render token and the install script renders
  them with an absolute path.** Each tracked unit holds
  `@REPOACH_HOME@` where the checkout path stood (`WorkingDirectory=`,
  `ExecStart=`, `StandardOutput=append:`, `StandardError=append:`); the
  tracked files are templates and are never copied as they are.
  `scripts/install_user_units.sh [--home <abs path>] [--dest <dir>]
  [--dry-run]` renders the three services and the two timers into
  `<dest>` (default `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user`),
  substituting the token with `--home` (default: the checkout the script
  lives in, `git -C "$(dirname "$0")/.." rev-parse --show-toplevel`;
  a relative `--home` is refused), creates `<home>/logs`, runs
  `systemd-analyze --user verify` on the five rendered files and
  refuses with exit 2 if it prints anything, then `systemctl --user
  daemon-reload`; it refuses to run when `systemctl --user` is
  unavailable, with the reason. `--dry-run` prints the rendered units
  to stdout and writes nothing. An operator who moves a checkout re-runs
  the script; an operator who prefers not to overwrite an installed
  unit writes a drop-in `<unit>.service.d/path.conf` holding literal
  absolute values (`WorkingDirectory=`, an empty `ExecStart=` then the
  new `ExecStart=`, the two `append:` paths), which the README shows.
  `deploy/systemd/README.md` documents the token, the script and the
  drop-in in place of its lines 4-12 and the three `cp …` blocks
  (`:30-33,46-49,70-73`). `…SP-MEMORY-BACKEND-LIVENESS_*.md:136-142`
  (four lines spelling `%h/<two segments>` for this checkout and for
  another operator service) cites the new convention and `<layout>`
  (D7). `Environment=PATH=%h/.local/bin:…`
  (`repoach-llm-proxy.service:28`) stays: `%h` alone is not a layout.
- D4. **The live units are not touched by this change.** The gateway
  runs copies under `~/.config/systemd/user/` (`README.md:31-33,46-49,70-73`); a
  merge changes the tracked files only. The owner's migration is the
  operator note O2 below, optional and outside the conditions.
- D5. **The recorder is another child.** Making
  `src/repoach/forge/recording.py` and `scripts/record_forge_fixture.py`
  write the D1 shape is SP-RECORDER-REDACTS-BY-CLASS; this spec rewrites
  the existing fixtures by hand to that shape (D2) so the conformance
  suite replays unchanged argv except the handle, and the recorder
  child then pins that a re-recording produces the same shape.
- D6. **The oracle of "no personal string in a fixture" is the class
  lint of SP-NO-PERSONAL-DATA-GATE, never a grep that spells the
  string.** The four oracles of F15 become
  `scripts/lint_no_personal_data.py --tree <fixture path>` →
  `personal-data: ok`, run with the maintainer's deny-list present (the
  secret in CI, the config file locally). Until that child lands the
  line is written and marked "judged when SP-NO-PERSONAL-DATA-GATE
  lands"; the verifier of this spec judges the four fixtures with
  `git grep -c` of the owner's strings on their own machine and writes
  the counts (all zero) beside AC2.
- D7. **Every edited spec is bumped.** `version` + 0.1 (or + 1 on the
  minor when the spec is ≥ 1.0), `updated: <date>`, and one amendment
  paragraph after the title: "Amendment (<v>, <date>): personal strings
  replaced by placeholders (SP-SCRUB-PERSONAL-DATA-AT-HEAD D2); the
  fixture oracle is the class lint (D6)." The 20 specs and their
  measured versions at `0be2a56`: SP-CONVENTIONS-GATE 0.2→0.3,
  SP-PIN-TOOLCHAIN 0.1→0.2, SP-CLAUDE-CODE-FIRST-CLASS 0.2→0.3,
  SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB 1.1→1.2, SP-REVIEW-DELTA-FIRST
  0.7→0.8, SP-REVIEW-SETTLED-STAYS-SETTLED 0.8→0.9, SP-CI-LEDGER-PERSISTS
  0.4→0.5, SP-GITLAB-NOTE-RECEIVER 0.6→0.7, SP-UNIVERSAL-REPO 0.2→0.3,
  SP-ANONYMISED-GAP-SIGNAL 0.1→0.2, SP-REVIEW-MEMORY-LOOP 0.1→0.2,
  SP-MEMORY-BACKEND-LIVENESS 0.1→0.2, SP-CI-SUPPLY-CHAIN-HARDEN 0.1→0.2,
  SP-REFUTER-JUDGES-WHAT-IT-CAN 0.3→0.4, SP-AUTOMERGE-EVENT-DRIVEN
  0.2→0.3, SP-FINDING-THREAD-IDS 0.2→0.3,
  SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF 0.3→0.4,
  SP-GATE-NAMES-FORMAT-SKEW 0.2→0.3, SP-RELEASE-PR-CI-RUNS-FOR-THE-BOT-ACTOR
  0.2→0.3, SP-SKIPPED-REQUIRED-CHECK-IS-NOT-GREEN 0.5→0.6.
  SP-REVIEW-FABLE-TIER is not edited (parent F21: its bump happened).
- D8. **The commit identity is set, and from the gate on it is judged.**
  The owner set the repository-local `user.email` to the GitHub noreply
  on 2026-11-18 (parent F11; the global config still holds the personal
  mailbox and is not this repository's business). The lint of
  SP-NO-PERSONAL-DATA-GATE judges the author and committer mailbox of
  every commit a pull request adds against the mailbox class (a noreply
  or a documentation domain), so the setting cannot silently lapse. The
  23 past commits stay. The pull request of this spec is the first
  judged: C6.

## End state

`git grep -n -P '(?<![A-Za-z0-9_$.-])/(home|Users)/[A-Za-z0-9_.-]+(?![A-Za-z0-9_.-])' -- . ':!tmp' ':!docs/specs/2026-11-18_SP-NO-PERSONAL-DATA-GATE_*'`
on `develop` prints exactly the four synthetic `HOME` lines of
`tests/unit/test_review_planner_cc.py:139,142` and
`tests/unit/test_secret_env.py:25,38` (declared in child 3's
allow-list, as is the excluded gate spec, whose worked examples spell
the pattern) and nothing else; the owner's `git grep` for each of
their strings, the layout string included, prints the five attribution
positions and nothing else; the three units verify on a
host with no checkout under the maintainer's layout; `pytest -q
tests/unit/test_forge_conformance.py
tests/unit/test_review_gate_forge_parity.py
tests/integration/test_generated_artefacts_replay_yoob_322.py
tests/unit/test_ci_portability.py tests/unit/test_owner_commands.py
tests/unit/test_finding_posts.py` is green; `REPOACH_ALLOWED_ACTORS`
is set and every workflow fallback is `'[]'`; the live gateway's units
are byte-identical to before the merge until the owner runs O2.

## Easy solution rejected

A `sed` over the tree with the owner's strings and no written rule:
rejected, the next recorded fixture brings them back (F16 keeps
`login`, `name`, `username`) and the next spec quotes them in an
oracle (F15 did, three times). Keeping `%h/<layout>/repoach` with a
comment "adjust if different": rejected, it is what the README says
today (`:8-10`) and every operator edits a tracked file.
`Environment=REPOACH_HOME=…` with `${REPOACH_HOME}` in the settings:
rejected by measurement (Context), systemd 255 refuses it in
`WorkingDirectory=` and the unit does not start. A `REPOACH_HOME` read
by the Python code at runtime: rejected, the units need the path before
any Python runs (`ExecStart=` names the interpreter). Replacing the
handle in `SECURITY.md` and `docs/languages.md`: rejected by parent
D3, a project needs a named contact and a maintainer column. Landing
the empty fallback and setting the variable afterwards: rejected, the
pull request would be the last to run CI (F24).

## Hard part

The forge fixtures are replayed argv-for-argv (`forge/recording.py:
285-300`): the handle sits inside recorded `args` (`collaborators/<handle>/
permission`, `members/all?query=<handle>`) and inside JSON stdout; the
tests that build expectations (`test_forge_conformance.py:595-608`)
and the fakes that answer them (`tests/forge_fakes.py:109`,
`tests/merge_wait_fakes.py:249-297`) must all move to the same class
word in one pull request, or the conformance suite is red on the first
argv. The second hard part is proving the units on CI's systemd rather
than asserting their text: `systemd-analyze --user verify` refuses a
unit whose `ExecStart=` names a missing executable (Context), so the
test renders into a temporary directory whose fake checkout holds
executable stubs at `.venv/bin/repoach` and `.venv/bin/python`, and
skips with the reason only when `systemd-analyze` itself is absent.

## Goals

- G1: every occurrence of the parent's F1–F9 outside the five attribution positions is rewritten under D1 (D2).
- G2: the five unit files carry the render token, the install script renders and verifies them, the README documents it (D3).
- G3: the four spelled-string oracles are replaced by the class lint (D6); every edited spec is bumped (D7).
- G4: the workflows' actor fallback is empty and pinned by a generic test (D2); the docs say the variable is required; the variable exists before the fallback lands (O0).

## Non-Goals

- NG1: touching the installed units (D4; O2 is the owner's).
- NG2: rewriting history (parent D4).
- NG3: the recorder (SP-RECORDER-REDACTS-BY-CLASS), the generic lint (SP-NO-PERSONAL-DATA-GATE), the secret scan, the release.
- NG4: renaming `yoob_322` or the plan file (parent D6).

## Assumptions

- A1: systemd ≥ 250 on every host the units target (the floor SP-GITLAB-NOTE-RECEIVER G4 set); CI's runner has `systemd-analyze` (SP-GITLAB-NOTE-RECEIVER's Tests table runs it today, seven rows).
- A2: `repoach bench score` is run from the checkout root (`bench/corpus/README.md:10` runs it so).
- A3: the owner approves parent D3 (attribution kept) before this pull request opens.
- A4: the owner gives the go for O0 at delivery; the delivering agent runs it and writes AC4.

## Interface

Inputs:
- `deploy/systemd/*.service`, `*.timer`: templates holding `@REPOACH_HOME@`.
- `scripts/install_user_units.sh [--home <abs path>] [--dest <dir>] [--dry-run]`: renders the five files, verifies, reloads.
- `tests/unit/test_ci_portability.py`: the two actor-literal assertions (generic absence, empty fallback).

Outputs:
- `install_user_units.sh`: one `installed: <path>` line per rendered file, `verified: <unit>` per unit, exit 0; exit 2 with the reason when `systemctl --user` is unavailable, when `--home` is relative, or when `systemd-analyze --user verify` prints anything (its output is relayed).
- `--dry-run`: the five rendered files on stdout, separated by `# --- <name>` lines, nothing written.

Errors:
- exit 2 (`install_user_units.sh`): the three reasons above, each named on stderr.

## Behavior

### Nominal

The delivering agent runs O0 after the owner's go and writes AC4; the
implementer rewrites D2 file by file, runs the six suites of the End
state, renders the units into a temporary directory with
`--dry-run` and with `--dest`, verifies them, bumps the twenty specs
(D7), and opens one pull request titled `refactor(repo): replace
personal strings by placeholders`.

### Edge cases

- A fixture value is both a login and a reason text (`owner-resolved by <handle>: …` in `test_owner_commands.py:211`) → the test builds the string from `MAINTAINER`, the fixture holds `maintainer`.
- A spec cites a path under the maintainer's home as evidence of a measurement (`…SP-PIN-TOOLCHAIN_*.md:39`) → the path becomes `<repo>/.venv`, the measurement stays.
- The checkout is not a git work tree (an extracted sdist) and `--home` is absent → the script refuses with the reason; the sdist of SP-RELEASE-0-1-1-IS-CLEAN ships no `deploy/`, so this is a fresh-clone case only.

### Failure scenarios

- `ReplaySpawn` red on an argv → the fixture's `args` and the test's expectation disagree on the class word; both are in this pull request.
- `systemd-analyze --user verify` prints a line on the rendered units → the script exits 2 and relays it; nothing is reloaded; the test is red with the same line.
- O0 skipped → the first pull request after this merge runs no job; `gh variable list` shows the omission; the fix is O0, not a YAML edit.

### Operator steps

- O0 (the delivering agent after the owner's go, once, before this pull request opens): `gh variable set REPOACH_ALLOWED_ACTORS --body "[\"$(gh api user -q .login)\"]"` — the handle is read from the authenticated account, never typed. Oracle: `gh variable list | grep -c '^REPOACH_ALLOWED_ACTORS'` → `1`, and `gh variable get REPOACH_ALLOWED_ACTORS | python -c 'import json,sys; v=json.load(sys.stdin); print(type(v).__name__, len(v))'` → `list 1`.
- O1 (owner, done 2026-11-18): the repository-local `user.email` is the GitHub noreply; `git config --get user.email` ends with `@users.noreply.github.com`.
- O2 (owner, optional, live gateway): `scripts/install_user_units.sh --home <the live checkout path>` then `systemctl --user restart repoach-llm-proxy.service` and `systemctl --user list-timers`; before it, `systemctl --user show -p WorkingDirectory repoach-llm-proxy.service` prints the old layout path, after it the live checkout path. Skipping O2 leaves the gateway running on the old copies with no functional change.

## Tests

| Test | What it pins | Mutation that must turn it red |
|---|---|---|
| `tests/unit/test_user_units_take_an_install_path.py::test_units_carry_the_token_and_no_layout` | D3: each of the three services contains `@REPOACH_HOME@` in `WorkingDirectory=` and `ExecStart=`; no line of the five unit files or the README contains `%h/` except a line starting with `Environment=PATH=`, so no layout under the home survives | put a layout back |
| `::test_rendered_units_verify_on_this_systemd` | D3: `install_user_units.sh --home <tmp checkout> --dest <tmp dir>` on a fake checkout holding executable stubs at `.venv/bin/repoach` and `.venv/bin/python` → exit 0, five `installed:` lines, three `verified:` lines, and `systemd-analyze --user verify <tmp dir>/*.service <tmp dir>/*.timer` run by the test prints nothing (skipped with the reason when `systemd-analyze` is absent); the rendered files contain no `@REPOACH_HOME@` | leave a token unrendered, or point `ExecStart=` at a missing file |
| `::test_install_script_refuses_without_user_systemd_and_a_relative_home` | D3: with `PATH` holding no `systemctl`, exit 2 and the reason; with `--home x`, exit 2 and the reason; `--dry-run` writes nothing and prints five `# --- ` headers | exit 0, or write under `--dry-run` |
| `tests/unit/test_ci_portability.py::test_actor_allowlist_has_no_login_fallback` | D2: every `fromJSON(vars.REPOACH_ALLOWED_ACTORS \|\| …)` in `.github/workflows/*.yml` has the fallback `'[]'`, and no workflow contains `fromJSON('["` | put a handle back |
| `tests/unit/test_forge_conformance.py` (existing, re-pointed) | D2: every replayed argv and every expected `login`/`username` is `maintainer`; `test_forge_conformance.py:595-608` builds `members/all?query=maintainer`; no recorded `author` object has a `name` key | keep one `args` with the handle |
| `tests/integration/test_generated_artefacts_replay_yoob_322.py` (existing) | D2: green with `product.example.com` in fixture and test | change one of the two |
| `tests/unit/test_bench_score.py` and `tests/unit/test_bench_cli.py` (existing) | D2: `corpus_root` relative resolves from the working directory | an absolute path in the result fixture |

## Success conditions

- C1 (oracle: git, any machine; both greps exclude the specs of this family that spell the patterns as worked examples, which child 3's allow-list declares): `git grep -n -P '(?<![A-Za-z0-9_$.-])/(home|Users)/[A-Za-z0-9_.-]+(?![A-Za-z0-9_.-])' -- . ':!tmp' ':!docs/specs/2026-11-18_SP-NO-PERSONAL-DATA-GATE_*'` → exactly four lines, `tests/unit/test_review_planner_cc.py:139`, `:142`, `tests/unit/test_secret_env.py:25`, `:38` (measured on this branch's tree 2026-11-18: those four plus the 6 maintainer files of parent F1, which go to 0; the excluded gate spec holds 5 example lines); `git grep -n '%h/' -- deploy docs ':!docs/specs/2026-11-18_SP-SCRUB-PERSONAL-DATA-AT-HEAD_*' ':!docs/specs/2026-11-18_SP-NO-PERSONAL-DATA-GATE_*' | grep -v -c 'Environment=PATH='` → `0` (measured on this branch's tree: 16 lines in 5 files — the three units, the README and `…SP-MEMORY-BACKEND-LIVENESS_*.md:136-142` — which D2 and D3 rewrite; the two excluded specs hold 7 lines describing the pattern).
- C2 (oracle: the owner's `git grep` on their machine, counts written beside AC2 by the verifier): for each of the owner's strings, `git grep -l <string> -- . ':!tmp'` names only files among `LICENSE`, `pyproject.toml`, `SECURITY.md`, `docs/languages.md`, `docs/specs/*.md` (`author:` lines only, checked with `git grep -n <string> docs/specs | grep -v ':author: '` → 0); for the layout string, no file.
- C3 (oracle: pytest, CI): the six suites of the End state green, plus `tests/unit/test_user_units_take_an_install_path.py`.
- C4 (oracle: systemd, CI or a fresh machine): the rendered units of the test (C3) verify with empty output; on a fresh machine, `scripts/install_user_units.sh --home "$PWD" --dest "$(mktemp -d)"` from a checkout with a `.venv` → exit 0 and three `verified:` lines.
- C5 (oracle: git): `for f in <the 20 specs of D7>; do grep -m1 '^version:' $f; done` shows each bumped value; `git diff origin/develop -- docs/specs | grep -c '^+version:'` → `20`.
- C6 (oracle: git): `git log --format=%ae origin/develop..HEAD | grep -v -c '@users.noreply.github.com$'` on the pull request's head → `0`.
- C7 (oracle: gh, written beside AC4 with the date): `gh variable list | grep -c '^REPOACH_ALLOWED_ACTORS'` → `1` before the pull request opens.

## Architecture Impact

- Adds dependency: SP-SCRUB-PERSONAL-DATA-AT-HEAD -> SP-UNIVERSAL-FORGE (`tests/fixtures/forge/` and `test_forge_conformance.py` are its, D2)
- Adds dependency: -> SP-PRECISION-MEASUREMENT (`bench/results/*.json` D2 is its)
- Adds dependency: -> SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ (`tests/fixtures/generated_artefacts/` and its replay test, D2)
- Adds dependency: -> SP-OWNER-SETTLES-A-FINDING, -> SP-FINDING-THREAD-IDS (their tests and spec text carry the handle, D2)
- Adds dependency: -> SP-CI-JOBS-NO-OP-WHEN-NOT-AFFECTED (`.github/workflows/ci.yml` fallback, D2), -> SP-CI-SUPPLY-CHAIN-HARDEN (the actor allow-list it introduced and `docs/security_posture.md` it wrote, D1, D2), -> SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF (`tests/fixtures/merge_wait/*.yml` and its text, D2), -> SP-AUTOMERGE-EVENT-DRIVEN (its text, D2)
- Adds dependency: -> SP-CONTRIBUTING (`docs/languages.md` allow entry, D2)
- Adds dependency: -> SP-GITLAB-NOTE-RECEIVER (the example section and AC3, D2; the systemd floor, A1), -> SP-MEMORY-BACKEND-LIVENESS (the README convention it cites, D3)
- Adds dependency: -> SP-CI-LEDGER-PERSISTS, -> SP-REVIEW-DELTA-FIRST, -> SP-REVIEW-SETTLED-STAYS-SETTLED (their oracles, D6), -> SP-UNIVERSAL-REPO, -> SP-ANONYMISED-GAP-SIGNAL, -> SP-REVIEW-MEMORY-LOOP, -> SP-CONVENTIONS-GATE, -> SP-PIN-TOOLCHAIN, -> SP-CLAUDE-CODE-FIRST-CLASS, -> SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB, -> SP-REFUTER-JUDGES-WHAT-IT-CAN, -> SP-GATE-NAMES-FORMAT-SKEW, -> SP-RELEASE-PR-CI-RUNS-FOR-THE-BOT-ACTOR, -> SP-SKIPPED-REQUIRED-CHECK-IS-NOT-GREEN (their text, D2, D7)
- New resource: `format:redaction-rule` (the D1 table). No table. No new cycle: this spec owns the five unit files, the README, one new test, one new script and two frontier files (`tests/unit/test_ci_portability.py`, `docs/security_posture.md`, unowned at `0be2a56` per `Registry.owner_of`), none imported by anything; every edge above is a document or fixture edit, not an import.

## Diagram

```mermaid
flowchart LR
    V[O0 gh variable set] --> F[fixtures, docs, specs rewritten D2, fallback '[]']
    I[inventory F1-F9] --> R[redaction rule D1]
    R --> F
    U[units carry @REPOACH_HOME@ D3] --> S[install script renders, verifies]
    S -.optional O2.-> L[live gateway]
```

## Acceptance Criteria

- [ ] AC1: C1 and C5 as written; `pytest -q tests/unit/test_user_units_take_an_install_path.py tests/unit/test_ci_portability.py tests/unit/test_forge_conformance.py tests/unit/test_review_gate_forge_parity.py tests/integration/test_generated_artefacts_replay_yoob_322.py tests/unit/test_owner_commands.py tests/unit/test_finding_posts.py tests/unit/test_bench_score.py tests/unit/test_bench_cli.py` → green.
- [ ] AC2: C2's counts written here by the verifier; C4's fresh-machine output written here; C6's `0`.
- [ ] AC3: `repoach arch check --base origin/develop` → `edge-honesty: ok`; `repoach arch graph --check 2>&1 | grep -c '^dependency cycle'` → `27`; the `ownership conflict` lines are the five of the parent's F20.
- [ ] AC4: C7 written here with the date, before the pull request opens; the live gateway, before O2: `systemctl --user is-active repoach-llm-proxy.service` → `active` after the merge, written here by the owner.

## Open Questions

- None. A3 (parent D3) is the owner's default; a flip amends D2's last bullet before the pull request opens.
