---
id: SP-RECORDER-REDACTS-BY-CLASS
title: The forge recorder replaces every login by its class word, drops names and avatars, makes user ids synthetic at record time, and refuses a fixture it cannot classify
version: 0.28
status: approved
author: agent
created: 2026-11-18
updated: 2026-11-24

owns:
  code: [scripts/record_forge_fixture.py, scripts/redact_forge_fixtures.py, tests/unit/test_forge_recording_redaction.py, tests/unit/test_forge_recording_acceptance_corpus.py, tests/unit/test_forge_closure.py, tests/unit/test_forge_recorder_sweep.py]
  resources: []

depends_on: [SP-UNIVERSAL-FORGE, SP-SCRUB-PERSONAL-DATA-AT-HEAD]
provides_to: []

constraints:
  class_words: [maintainer, author, bot, agent, service]
  exit_unclassified: 2
---

# The forge recorder redacts by class at record time

Amendment (0.2, 2026-11-18): the verifier's NO-GO on 0.1 re-measured.
The forges read `web_url` (`gitlab.py:223,982,1021`), `url`
(`github.py:501`) and `name` (`github.py:281,1776,1809`,
`gitlab.py:1016`, check-run and job names), so those keys are not
dropped: URL values are redacted by class with their path shape kept
(D1b), and `name` is dropped only inside a user object, like `id`
(D1). A test replays a redacted recording through the forge readers.

Amendment (0.3, 2026-11-18): the verifier's NO-GO on 0.2. `id` is
kept everywhere: `gitlab.py:728` builds `Review(id=_int(user.get("id")))`
from `approved_by[].user`, and `reviews_two_approvals.json` expects
11 and 12 (F1c); an id is an opaque number in none of the owner's
classes. Only `name` is dropped in a user object. The user-object keys
the readers consume are swept (F1c) and the replay test covers
approvals.

Amendment (0.4, 2026-11-18, reconstructed from git, commit `70b0cf1`
"approve the personal-data cleanup family"): status `reviewed` →
`approved`; no other line changed. The text of this entry was not recorded
at the time and is written here at 0.8 from `git diff 77817ab 70b0cf1`.

Amendment (0.5, 2026-11-18, reconstructed from git, commit `06b82c3`
"hold the recorder regexes equal to the gate and fix D2"): D3 gained
"and held equal by a test in SP-NO-PERSONAL-DATA-GATE" after "a comment
naming their source"; no other line of this spec changed. The text of this
entry was not recorded at the time and is written here at 0.8 from
`git diff 70b0cf1 06b82c3`.

Amendment (0.6, 2026-11-18, the implementer, measured on `origin/develop`
`7fd31af`): D4 was false. Parent F27: after child 1 the fixtures still
held 115 `url`/`web_url`/`html_url` values on the real forge hosts, in 33
files (`git grep -o -E '(url|web_url|html_url)\\?": \\?"https://(github\.com|gitlab\.com)' 7fd31af -- tests/fixtures/forge`
→ 115, `-l` → 33; the parent counted 34), and 149 forge-host URLs in 41
files once `api.github.com`, `link` values, argv, stdin, stderr and text
stdout are counted (`docs.*` links excluded). The namespace SCRUB wrote
(`maintainer`) is itself a real account on those hosts. So D4 and G4 are
replaced: the fixtures are rewritten by the recorder's own functions,
offline (`scripts/redact_forge_fixtures.py`, D4 below), and C2 is a fixed
point instead of an empty diff. Other claims measured wrong against the
code and corrected here: (a) `RECORDED_FIELDS` is not "every key the forges
read" — `_squash_body_from_view` reads `commits[].messageHeadline` and
`messageBody` (`github.py:170-175`); applying `redact_payload` to
`github/merge.json` dropped them and turned `test_merge[github]` red, so
the three keys are added (D1 no longer leaves `RECORDED_FIELDS` unchanged);
(b) a user object keeps `state` beside `login`/`username` and `id`
(GitLab), so "exactly what the readers consume" is false; its URL keys
(`web_url`, `html_url`, `url`, a profile link) are now dropped with `name`;
(c) GitHub's `gh pr checks` link is under `link`, a URL key the spec did not
name: `link` joins `url`/`web_url`/`html_url`; (d) URLs also stand in free
text (a `glab` error, `gh pr create` output, a check link passed as a method
argument), so text URLs are redacted too, RFC 2606 and loopback hosts kept;
(e) the hand-written placeholder logins `alice`, `bob`, `dev`, `devops` are
also real accounts, and one GitLab project-bot username names a real project
id: the transform maps them to `author-<n>` and `service`; (f) the recorder
also classifies the method's arguments and the forge's bot login (`bot`),
since replay compares argv and trust compares the bot login; the result is
recomputed by replaying the redacted exchanges. The F1 line numbers are those
of `0be2a56`.

Amendment (0.7, 2026-11-18, owner decision on O1): a user `id` is still
kept as a key, but its value is remapped to a synthetic one (D1c). The
readers consume the GitLab approval user id (`gitlab.py:728`) only as an
opaque number, so a stable synthetic value replays identically. Measured on
the fixtures at `fc8df78`: 58 user-object ids, 52 of them real, 10 distinct
accounts before and after.

Amendment (0.8, 2026-11-18, the verifier's NO-GO on `4f78a96`, every item
fixed, measured on the branch over `origin/develop` `7fd31af`):
(a) defect 1 — the test held a salted SHA-256 of the maintainer's two real
ids, and the salt was in the tree: enumerating every candidate id reverses
it in about a minute, the very argument D1c makes against a hash. The
hashes are gone from the branch's whole history (the commit that added them
was rewritten before any push); the real ids are read at test time from the
maintainer's untracked deny-list, SP-NO-PERSONAL-DATA-GATE D3's own
mechanism, and the test skips with that reason when the deny-list is absent
(D1c, D3). (b) defect 2 — End state, D1, G1 and the Hard part still said
"every id is kept/as recorded" and D1 that `RECORDED_FIELDS` was unchanged;
both were false since 0.6/0.7 and are corrected below, as is
`docs/security_posture.md`. (c) defect 3 — the offline map was keyed by
the number alone, so a GitHub and a GitLab account with the same number
merged, and `apply_known` remapped any result `id` equal to a user id seen
earlier; the map is now keyed by `(forge, id)` and a result id follows its
account only where it is one, a GitLab `Review.id` (D1c). (d) defect 4 —
one map per fixture (M11) and a sequence restarting at 1001 over the
existing 1001–1004 (M13) both survived mutation; two tests over
temporary corpora now kill them (Tests). (e) the 10000 threshold kept any
small real id silently (P2): a fixture now declares its synthetic ids under
`synthetic_user_ids`, the recorder writes the declaration, and an
undeclared id is remapped whatever its size (D1c). (f) a recorded `source`
named the checkout's folder (`m3`, the private repository's name, or the recorder's own) and D3
ran before the field existed (P1): `source` names the forge, the offline
transform rewrites the 30 that did, and D3 judges the whole fixture (D3,
D4). (g) the real-account rule was implicit and uneven — the recorder
mapped `repoach-bot` to `bot` while the offline transform kept it, and a
GitLab project bot became `bot-<n>` in one and `service` in the other
(P3): one rule, D2. (h) an `@<handle>` in free text was neither replaced
nor refused (P5): it becomes a class word, in both tools (D2). (i) the
tests outside the fixtures still held `gitlab.com/maintainer/<private repository>` and
its scp and API forms, derived from a real remote and naming the real
account `maintainer` (P4): this spec takes them on (D5). Wrong facts
corrected: AC1's C1 count was 131, the measured count was 134; the
out-of-fixture forge URLs are 56 occurrences on 54 lines (54 without the
two `docs.*` links), not 52; `LEGACY_LOGINS` also held `owner`, which D4
did not list (it is now gone: D4 states the general rule); a `test:`
commit edited this spec at 0.7 without a bump (`4f78a96`), which this entry
supersedes.

Amendment (0.9, 2026-11-18, the second verifier's NO-GO on `58b928a`, every
item fixed, measured on the branch over `origin/develop` `7fd31af`):
(a) defect 1 — the suite's autouse `_hermetic_xdg_dirs` points
`XDG_CONFIG_HOME` at a scratch directory, so under pytest `read_denylist()`
never saw `~/.config/repoach/personal-denylist`: with the real file present
(10 entries, mode 600) the deny-list test skipped, and D1c, O2 and C1, which
say the file and the variable are merged, were false under pytest. The
test now asks for `real_user_config_dir`, a session fixture of
`tests/conftest.py` that names the directory from `XDG_CONFIG_HOME` as the
process found it, before the override; `read_denylist(config_dir)` reads the
file there; every other test stays hermetic; the skip names the path it
checked (home written `~`); a test pins the file route (D1c, D3).
(b) defect 2 — `clone git@github.com:<login>/r.git` kept the login and the
real host: `_login_pattern` refused a token after `:` (to keep `:owner`),
and text URLs were `http(s)` only. An scp remote `<user>@<host>:<ns>/<repo>`
and an `ssh://`/`git://` URL now get the placeholder host, namespace and
repository in both tools; a login after `<host>:` is a token (not after `/:`
or `::`); and D3 refuses any URL or remote left on a host the redaction
rewrites (`unredacted host at <path>`). (c) preference 1 — `@pytest.fixture`,
`@dataclass` and `@property` were handles, and the bare word was then
replaced everywhere (`import dataclass` became `import author-3`): a dotted
name, a decorator call, a standard-library decorator and anything in a
Markdown code span are no handle, and a login known only from a mention is
replaced in its `@<handle>` form only (D2). (d) preference 2 — a declared
id was trusted at any value, so a real one could be declared: only
`1001..1999` is trusted, a declaration outside it is remapped, and the six
hand-written ids 0.8 declared (3, 7, 8, 9, 11, 12) are now remapped like any
real id, so the D4 reproduction declares nothing (D1c, D4). (e) preference
3 — commit `2122754` (`test(forge)`) edited this spec at 0.7 without a bump;
its row was folded into the 0.8 `docs(specs)` commit before any push. (f)
preference 4 — a login equal to a git word (`main`) is replaced in every
value, branch fields and prose included; that is the safer option and is
stated in D2. (g) preference 5 — the private project name stood in 30 URLs
and a GitLab project object of the fixtures: the repository segment of
every recorded URL is `project`, and a repository object's `name`/`path`
too (D1b). (h) preference 6 — the repository secret
`REPOACH_PERSONAL_DENYLIST` reaches the unit-test step of `ci.yml` (O2).
Wrong facts corrected: D1c, O2 and C1 said the file was read under pytest,
false until (a); D2's claim that `@pytest.fixture` in a diff "stays" held
for diffs but not for the same decorator in prose, true after (c); AC1's
C1 figure is now measured by the file route.

Amendment (0.10, 2026-11-18, the third verifier's NO-GO on `7066d3a` and
the analyst's class plan, every class fixed, measured on the branch over
`origin/develop` `7fd31af`): rounds 2 and 3 each found a known value in a
string position the redactor's grammar had not foreseen (after `:`, after
`.`, uppercase, a userinfo, a display `name`, a query), and the refusal was
built from the same grammar. 0.10 changes what is judged. (K1, I1) one
token boundary, `TOKEN_CHARS` and `token_pattern`, for the login replacer,
the `@` replacer and the deny-list; the `:owner` exception is gone, the
tools' own literals (`:owner`, `:repo`, `:id`, `/dev/null`) are declared
instead (D2). (K2, I2) every recogniser of a scheme, host or login is
`re.I`, the case-sensitive ones named by a test; the oracle casefolds. (K3,
I3) a closure oracle by needles, built from the raw exchanges and from
facts and independent of every redaction regex, judges the built fixture
casefolded and URL-decoded; the recorder and the offline transform exit 2
on a hit (D3, D4). (K4, I4) a repository object's `name`/`path` are
`project` by key and its `description` is dropped (D1b). (K5, I5) an scp
remote is normalised through `redact_url`, a userinfo is never kept, the
query and any fragment but a comment anchor are dropped (D1b). (K6, I6)
each exit path and grammar alternative has a test that names it (Tests).
(K8) both scripts log to stderr, the deny-list is opaque, the secret
reaches one step (D3, D6). Owner decisions of 2026-11-18 (Open Questions):
Q1, the client project's code identifiers in recorded prose and diffs (12
lines in 10 files: `git grep -c -i yoob 7066d3a -- tests/fixtures/forge`)
were rewritten once by hand to `acme` in the same case, and their 21 forms
added to the maintainer's deny-list file and secret; Q2, a repository id
is synthetic in `2001..2999` (D1c); Q3, the residual is named (NG4); Q4,
masking measured (D6). Wrong facts corrected: AC1's "21 of 22" was typed
from memory, the 0.10 list is in AC1 by id with its command; D6's "GitHub
masks the value" held for the whole value only, now measured line by line;
D2 called a project bot's id "a real project id" while the same number
stood as the project `id` of `gitlab/branch_protection.json`, now `2001`; "98 segments" had no command,
AC1 names one; D1b said the private name "belongs in the deny-list" while
the list did not hold it, it does now; and the commit that redacted the
fixtures (`24cce22` before the rewrite) added 21 lines naming the private
repository that a later commit removed: the unpushed history was rebuilt
so that no commit adds one (AC1, the per-commit count). Two hand edits
precede the offline transform in the D4 reproduction: Q1's, and the three
`source` fields that cited documentation by a URL on a forge host now cite
it by title, since the oracle judges every forge-host string.

Amendment (0.11, 2026-11-18, the fourth verifier's NO-GO on `5393cb6`,
every item fixed, measured on the branch over `origin/develop`
`7fd31af`). (R1) The oracle shared the replacer's token boundary, in
which `-` and `_` were token characters, so `<login>-patch-1` (GitHub's
web-edit branch, through `headRefName`), `x-<v>`, `_<v>` and `<v>_x`
escaped both, for every needle kind: `TOKEN_CHARS` is now letters and
digits only, for the replacer, the `@` replacer, the deny-list and the
oracle alike, so the replacer clears the branch name and the oracle's
doubt resolves toward refusing (D2). (R2) Logins under keys the
redaction drops never became needles: `raw_logins` reads them from the
raw decoded stdout, as needles and as roles (D3). (R3) Segments came only
from the public forges and the remote's host: they now come from the
recorded project (the remote's path, an ssh alias's included, the argv's
API paths), from every URL on the remote's host when it is a private
one, and from every URL on any private host when the remote names none
(D3). (R4) A node id inside a longer string was not decoded: every
node-id-shaped run is, in `residual` and `denylist_hit` alike
(`embedded_id_numbers`). (R5) Dict keys were never judged: every key
outside `CODE_KEYS` is, and a path names it by ordinal. (R6) A
third-party link made an ordinary pull request unrecordable (PR #316's
`conversation_comments` exited 2 on `residual segment 4 at forge`): a
public third party's project is no needle, and the recorder's
`CONSTANT_FIELDS` and `source` head are judged by the deny-list only.
The oracle also judges every encoded and lookalike form (`oracle_forms`:
double percent, HTML entities, `\u` escapes, full-width, invisible and
lookalike letters), and finds hosts with its own reader (`locations`),
so the independence claim is now stated exactly (D3). (R7) The
verifier's eight survivors (V4-05, 06, 07, 08, 10, 11, 13, 14) and V4-16
are killed by named tests; the new code has its own mutants (AC1).
Two findings of this round's own re-measurement are fixed as well: with
`-` a delimiter, a placeholder login became a word of a spec identifier
(`SP-OWNER-…`, the D4 reproduction refused) and a raw login a word of the
kept `github-actions[bot]` (a live recording refused); spec identifiers
and the non-person logins are declared literals, and the deny-list judges
the text with the literals kept. Wrong
facts corrected: AC2's explanation of the 0.10 refusal (it was the
segment `github` hitting `forge`, not a deny-list entry); the claim that
a later round could only find a missing needle source (D3); "independent
of every redaction regex" (D3, now exact); AC1's "each of the seven
needle sources dropped" (the remote-host segment source had no mutant);
and D5, which did not declare `tests/merge_wait_fakes.py` (`e7d23f1`) or
`docs/security_posture.md`. C3 now lists the six methods, each with its
allowed outcome.

Amendment (0.12, 2026-11-18, owner decision on the threat model and the
fifth verifier's NO-GO on `1b90c23`, every realistic defect fixed,
measured on the branch over `origin/develop` `7fd31af`). Owner decision:
the threat model is **accidental leaks** from real pull requests, not an
adversary crafting text on purpose to evade the recorder (new **Threat
model** section below). NG4 is extended to name, as accepted and out of
scope, the adversarial forms: Unicode confusables of any case (Cyrillic,
Greek, full-width) and invisible characters (D1), Markdown-split or
escaped values (`**zqx**jdoe7`, `zq\_jdoe`), values in non-node-id
base64, and JSON-in-a-string escapes (D8). What 0.11 already does for
these forms is kept intact without extending it, and every claim that
they are caught in general is dropped. Realistic defects fixed: (D2)
`embedded_id_numbers` scans current-format `U_…` runs independently of
base64 runs and finds the legacy `MDQ6VXNl` prefix anywhere in a run
(URL paths, branch names, prefixed or suffixed strings), in both
`residual()` and `Denylist.hit()`; (D3) spec-id literals in
`CLI_PLACEHOLDER_RE` are bounded to actual `docs/specs/` spec ids
(`SPEC_IDS`, 329 ids) so invented `SP-<value>` tokens never hide a real
login, segment or id, deny-list judgement keeps literals intact, and
free-form `case` is removed from `CONSTANT_FIELDS` (`forge` and `method`
only); (D4) `path_identity` (plus `_repository_root` and `_api_project`)
collapses empty segments from double slashes (`org//repo`, `repos//o/r`,
`users//u`) so segments never shift or leak; (D5) `classify_calls` (and
offline `_exchanges`) runs `redact_urls_in_value` on `call["args"]`, and
redacts URLs and noreply mailboxes before `classify_logins`; (D6) GitHub
and GitLab noreply mailboxes (`@users.noreply.github.com`,
`@users.noreply.gitlab.com`, self-hosted `@users.noreply.<host>`) redact
their local part and host by class (`author@users.noreply.forge.example.com`
/ `bot@users.noreply.forge.example.com`) instead of failing as
`generic class mailbox`, `_personal_mailbox` requires `/` after `:`
before skipping an scp remote (catching `real.person@private-mail.org:25`
and `:hello`), `locations()` rejects un-led code expressions
(`self.ms/1000`, `README.md/foo`, `foo.py:80/bar`) and accepts a DNS
trailing dot, and `needles()` infers the forge host from `URL_KEYS`
under an ssh-alias remote so third-party links like `docs.python.org` do
not raise `NeedleError`; (D7) all realistic surviving V5 mutants
(`V5-03`, `06`, `08`, `10`, `11`, `14`; `V5-07` concerns the confusable
table, now NG4) and 11 new 0.12 mutants are killed by named tests (AC1).
Wrong facts corrected: D1b now lists `link` beside `url`/`web_url`/
`html_url`; D2 and the diagram reflect that `redact_payload` runs before
`classify_logins`; D2, Edge cases and the Tests table no longer claim
`main-fix` or `_jdoe`/`jdoe_x` stay or that `-` is a word character
(false since 0.11 R1); D5 names the actual commit count for
`docs/security_posture.md` (5 at 0.11, 6 at 0.12, not 4) and marks the
0.8 out-of-fixture URL count by version; AC1 notes the 0.11 `grep`
command's `94 project` / `1 project\` quirk vs the 0.10 command's
`99 project`.

Amendment (0.13, 2026-11-19, the sixth verifier's NO-GO on `83ab01f`,
every item fixed, measured on the branch over `origin/develop`
`7fd31af`). (R6-D1) Markdown delimiters — backtick, `*`, `|` in
`_URL_IN_TEXT_RE` and `_AFTER_HOST_RE`, and trailing boundary `~` and
`_` via `_URL_TRIM_TAIL` in `redact_urls_in_text`, `unredacted_host` and
`locations()` — terminate text URLs without being eaten, so backticked
RFC 2606 / loopback URLs stay unchanged and backticked/bold/table/tilde/
underscore self-hosted URLs yield clean segment needles; `_SCP_IN_TEXT_RE`
accepts an optional DNS trailing dot and `URL_SCHEMES` / `_URL_IN_TEXT_RE`
include `ftp`/`ftps`. (R6-D2) Any `<name>[bot]` login (such as
`dependabot[bot]`, `renovate[bot]`) shares one consistent class word
across its login key, `@<name>` mention and bare prose stem (in
`build_roles`, `classify_text`, `needles` and offline `legacy_roles`/
`_rename`), while `@github-actions` is skipped via `_names_no_one`;
`_MENTION_END` rejects `/[A-Za-z0-9_.-]` so npm scoped packages
(`@types/node`, `@vue/cli`, `@babel/core`, `@actions/checkout`) are not
mentions; `PYTHON_DECORATORS` is checked case-insensitively (`@Override`).
(R6-D3) `locations()` replaces the growing `_NON_HOST_TLDS` denylist with
a positive DNS TLD allowlist (`_KNOWN_HOST_TLDS`) plus `known_hosts`
(the forge/remote host), rejecting code expressions
(`config.dir/sub/file.toml`, `obj.total/count/100`, `this.base/api/v1`,
`components.tsx/Header/index`, `cfg.root/sub/file`) and wheel paths
(`*.dist-info/…`) while still finding real schemeless hosts
(`gitlab.corp-x.io/grp-x/proj-x`, trailing-dot form) and custom-TLD
known hosts; scheme- or user-led IPv4 URLs (`https://10.20.30.40/…`)
are also parsed by `locations()` and collected as host and segment
needles so self-hosted IPv4 mirrors cannot leak prose repo names.
(R6-D4) All 10 round-6 surviving mutants (`V5-11`, `R5-D2b`/`V6-03`,
`V6-01`, `V6-11`, `V6-17`, `V6-18`, `V6-22`, `V6-24`, `V6-25`, `V6-26`)
plus 17 new 0.13 mutants are killed in a detached git worktree with a
verified green baseline and strict `rc == 1` kill criterion (AC1).
(R6-D5) Numeric GitLab project ids in `api/v4/projects/<id>/…` URLs map
through the same `UserIdMap.for_projects()` (`2001..2999`), are declared
under `synthetic_project_ids`, and are collected by `project_id_values`;
offline `scripts/redact_forge_fixtures.py` also redacts `kwargs`, and
`redact_urls_in_text` rewrites system noreply mailboxes
(`noreply@github.com`, `noreply@<self-hosted>`) to
`noreply@forge.example.com`. (Preferences 2–4) `needles()` infers the
public forge host from `URL_KEYS` when `remote=""` so third-party links
like `docs.python.org` do not raise `NeedleError`; well-known public
reference hosts (`PUBLIC_REFERENCE_HOSTS`: `docker.io`, `ghcr.io`,
`golang.org`, `k8s.io`, `pypi.org`, `npmjs.com`, `www.npmjs.com`,
`registry.npmjs.org`, `pkg.go.dev`, `proxy.golang.org`,
`docs.github.com`, `docs.gitlab.com`) stay intact in free text
(`docs.github.com` and `docs.gitlab.com` are literals in
`CLI_PLACEHOLDER_RE` so they do not collide with `FORGE_HOSTS`);
`CONFUSABLES` docstring aligned with 0.12 NG4. (Preference 1 — RFC 2606
subdomain mailboxes, `actions@github.com`, 4-part versions `from … to …`,
and Windows `C:\Users\<unknown>\…` — belongs to the shared constants of
SP-NO-PERSONAL-DATA-GATE and is left unchanged here.) Wrong facts
corrected: 0.12 AC1's "26 of 26 mutants killed" was false (`tmp/r5/mutate.py`
omitted `prompts/` and crashed `rc=4` on unmodified HEAD; real count was
24/26, `V5-11` and `R5-D2b` survived); schemeless `10.0.0.5/…` is a
schemeless private IPv4 path, not CIDR-shaped, and is now named in NG4;
`fixture_needles` docstring corrected to state `min_length=1`.

Amendment (0.14, 2026-11-19, the seventh verifier's NO-GO on `b1f675d`,
every item fixed by invariant in one commit per defect class, measured
on the branch over `origin/develop` `7fd31af`). Five invariants are now
stated and enforced across code and tests:
(1) **URL termination**: a text URL ends at the first character that
cannot end a URL in prose — any trailing punctuation ``.,:;!?)]}>'"``
plus Markdown delimiters (`` ` ``, ``*``, ``|``, ``~``, ``_``) — and
that character is never eaten (`_URL_TRIM_TAIL`, `_split_url_tail`,
fixing R7-D2 `!` and Preference 5 `?` in `redact_urls_in_text`,
`unredacted_host` and `locations`).
(2) **Userinfo**: a URL userinfo (`user[:pass]@`) is NEVER kept,
whatever the host — `PUBLIC_REFERENCE_HOSTS` (`ghcr.io`, `pypi.org`,
…) and RFC 2606 hosts included — and `unredacted_host` flags any
unredacted URL userinfo (R7-D1).
(3) **Case-invariant host decisions**: kept-host URLs normalize scheme
and host lowercase on the original `parts.netloc` via `urlunsplit`
(`_normalize_kept_url`), replacing 0.13's dead `url.replace(parts.hostname, …)`
no-op, and `docs.github.com` / `docs.gitlab.com` match case-insensitively
in `CLI_PLACEHOLDER_RE`, so `Docs.GitHub.com`, `DOCS.GITHUB.COM` and
`Docs.GitLab.com` never corrupt or refuse (R7-D1).
(4) **`[bot]` identity**: any `<name>[bot]` login shares one consistent
class word across its login key and `@<name>` mention whatever the stem
length (`bot_mention_stem`: fixing `cla[bot]`/`@cla`, `bob[bot]`/`@bob`,
`ci[bot]`/`@ci` in both tools), and across its bare prose stem when
`>= NEEDLE_MIN_LENGTH` and outside class words and `_PLACEHOLDERS`
(`bot_stem`: preventing offline `project[bot]` from rewriting
`/org/project/`); offline `legacy_roles` checks `[bot]` case-insensitively
(`Dependabot[BOT]` → `bot`) (R7-D4).
(5) **GitLab API project ids**: documentation example ids on kept/doc
hosts (`gitlab.example.com`, `docs.gitlab.com`) and `< 4`-digit mock ids
(`/api/v4/projects/1`, `/42`) are not personal data and stay untouched
without `NeedleError`; real project ids (`>= 4` digits outside kept URL
spans) are synthesized (`2001..2999`) with or without the `api/v4/`
prefix (`glab api projects/<id>/…` in comments, `call["args"]`, top-level
`args`/`kwargs`, `stdin`, `stderr`, non-JSON `stdout` and `result`)
(R7-D3).
Preferences applied: (Pref 3) `needles()` infers `instance = inferred`
whenever `_raw_url_host` finds a host under `remote=""` (public or
self-hosted), removing the self-hosted `docs.python.org` `NeedleError`;
(Pref 4) `locations()` replaces the `_KNOWN_HOST_TLDS` heuristic with a
strict `known_hosts`-only rule for un-led schemeless hosts (`FORGE_HOSTS`,
remote/inferred host, and hosts already seen led by `//` or `@` in the
same fixture), preventing corruption of `Xcode.app/Contents/…`,
`config.dev/env/prod`, `ssl.ca/certs/root.pem`, `stream.io/read/write`
and `torch.net/layers/conv`, with unknown schemeless third-party hosts
documented in NG4; (Pref 5) trailing `?` preserved outside redacted URLs;
(`actions@github.com`) normalized to `noreply@forge.example.com` in
`_SYSTEM_NOREPLY_RE` (the other three Preference-1 items remain for
SP-NO-PERSONAL-DATA-GATE, whose regexes will be held equal by that
spec's tests once it lands).
Convergence & R7-D5: promoted the realistic samples of `tmp/verify5/`,
`tmp/verify6/` and `tmp/verify7/` into a permanent table-driven
regression corpus (`tests/unit/test_forge_recording_regression_corpus.py`,
51 parametrized rows); killed all 11 round-7 surviving mutants
(`V7-05`, `V7-07`, `V7-08` on real normalization, `V7-14`, `V7-17`,
`V7-18`, `V7-21`–`V7-25`) plus 9 exact round-6 survivors (with the exact
original `V5-11` form, never rewritten) and 14 new 0.14 mutants: 34/34
killed, 0 survived.
Wrong facts corrected: (1) SP-NO-PERSONAL-DATA-GATE equality tests are
stated in future tense ("will be held equal by that spec's tests once it
lands") in D3, A3 and reports; (2) 0.13's universal `<name>[bot]` stem
claim was false for `< 4`-char stems — now fixed and stated precisely in
D2 and `docs/security_posture.md`; (3) 0.13's "userinfo is never kept"
and "`docs.github.com` stays intact" claims were contradicted by
`a1e7c27` — now enforced by R7-D1; (4) 0.13's `tmp/r6/mutate.py` had
rewritten `V5-11` — 0.14's `tmp/r7/mutate.py` runs the exact original
`V5-11` form; (5) `synthesize_user_ids` / `record()` coverage of `args`,
`kwargs`, `stdin`, `stderr`, non-JSON `stdout` and `result` is now
directly tested (killing `V7-21` and `V7-22`).

Amendment (0.15, 2026-11-19, the eighth verifier's NO-GO on `55010db`,
rebased onto `origin/develop` `8ee2c38`, every defect class fixed by
design in one commit per class). Each overly broad or incomplete regex
is replaced with its own rule, stated here and enforced by targeted and
property tests:
(1) **Public reference hosts & GFM balanced parentheses (R8-D1, Pref 2)**:
replaced the 12-entry exact-match set with 18 curated registrable
suffixes in `PUBLIC_REFERENCE_HOSTS` (`crates.io`, `docker.io`,
`docs.github.com`, `docs.gitlab.com`, `docs.rs`, `ghcr.io`, `go.dev`,
`golang.org`, `k8s.io`, `mozilla.org`, `npmjs.com`, `npmjs.org`,
`pypi.org`, `python.org`, `readthedocs.io`, `readthedocs.org`,
`stackoverflow.com`, `wikipedia.org`) matched by `_is_public_reference_host`
(exact host or `.<suffix>`, trailing dot stripped, casefolded; `docs.github.com`
and `docs.gitlab.com` stay scoped to their docs subdomains so `github.com`
and `gitlab.com` are never matched). Unlisted hosts are rewritten (documented
residual: over-redaction is the safe direction). Kept URLs still strip
userinfo, lowercase scheme and host, and scan path and query for planted
needles and deny-list values. `_URL_IN_TEXT_RE`, `_AFTER_HOST_RE` and
`_strip_balanced_tail` apply GFM balanced parentheses so Wikipedia/MSDN URLs
(`https://en.wikipedia.org/wiki/Python_(programming_language)`) stay intact
while outer enclosing parentheses are preserved. Pinned by
`test_public_reference_hosts_match_by_suffix_and_balance_url_parens`
(`R9-D1b`) and `test_python_code_and_third_party_links_under_ssh_alias_are_not_corrupted_or_refused` (`R9-D1a`);
the trailing-dot and case normalisation of `_is_public_reference_host` and the
balanced-tail trimming of `_strip_balanced_tail` are pinned by
`test_url_tail_splitting_public_host_normalisation_and_balanced_tail_by_construction`
(`V9-01`, `V9-05`; 0.16 — 0.15 named the first test for these and it stayed
green under both mutants).
(2) **Project ids scoped to API shapes (R8-D2)**: numeric project ids are
taken only from `api/v<N>/projects/<id>` paths (`_API_PROJECT_ID_RE`,
`_API_VERSION_RE`, `path_identity`, handling `v3`/`v4`/`v5`/… cleanly
without `NeedleError` on `"v3"`) or `glab api` command
lines tokenised with `shlex` and argv lists (`_glab_api_endpoint`,
`_glab_api_text_id_spans`, `_GLAB_API_VALUE_FLAGS`, `_GLAB_API_ARG_PROJECT_ID_RE`,
`_is_glab_api_argv`, 0.16),
never from bare repository or file paths (`projects/2026/q1.md`,
`/projects/2025/architecture.md`), eliminating year-path corruption and
2026 PR refusals. Property-tested across years 2020–2030 and ordinary
folder paths in `test_api_project_ids_ignore_year_paths_and_support_api_versions`
(`R9-D2a`, `V8-17`; the `["glab", "api", …]` argv form by
`test_glab_api_shlex_parsing_quoted_flags_argv_and_unbalanced_fallback`, `V9-07`),
with argv and offline coverage pinned by
`test_api_project_ids_skip_doc_examples_and_cover_all_positions` (`R9-D2b`)
and `test_offline_transform_redacts_all_six_positions_and_non_json_project_ids` (`R9-D2c`).
(3) **Unified userinfo rule (R8-D3, WF6)**: one rule (`_RFC3986_USERINFO_CLASS`,
`_URL_USERINFO_PREFIX`, `_URL_USERINFO_BEFORE_RE`, `_USERINFO_AHEAD_RE`,
`_has_user_before`, `_allowed_userinfo`, and `Location.authenticated`)
governs `locations()`, `needles()`, `redact_url`, `redact_urls_in_text`,
`unredacted_host()` and SCP remotes on every host class (RFC 2606 and
public reference hosts included). It covers empty passwords (`token:@`),
every RFC 3986 userinfo character (`!`, `$`, `&`, `'`, `(`, `)`, `*`, `+`,
`,`, `;`, `=`, `~`, `%xx`, `:`), authenticated second-host segment
collection in `needles()`, and SCP `user@host:path` (redacting
`alice@example.com:org/repo.git` to `git@forge.example.com:org/project.git`
while allowing only `git@forge.example.com` and `git@<example>`). Pinned by
`test_userinfo_rule_covers_empty_passwords_rfc3986_chars_and_scp_hosts`
(`V8-06`, `R9-D3a`–`R9-D3d`).
(4) **URL termination, SCP delimiter and `//` line comments (R8-D4, Pref 1)**:
`_split_url_tail` trims prose punctuation and non-underscore delimiters
(`_URL_TRIM_PUNCT`) before checking dropped fragments and queries so
`#__init__` and `?q=__main__` leave no stray `__` (while matching paired
leading `_` delimiters via `open_under`); `_SCP_IN_TEXT_RE` requires an
alphanumeric first user character so italic `_git@host:ns/repo.git_` never
eats its opening `_`; `_is_slash_led` accepted a `//host` unless a regex
said it followed a comment (`_COMMENT_SLASH_BEFORE_RE`, replaced in 0.16 by the
positive URL-context rule: a protocol-relative `//host/path` is read only in
a whole JSON or decoded structured string value, after `="` / `='` / `=`,
in `url(` or `](`, or after `<`; everywhere else `//` starts a comment and
the text stays byte-identical). Pinned by
`test_url_termination_preserves_dunder_fragments_scp_delimiters_and_line_comments`
(`R9-D4a`–`R9-D4c`) and, at 0.16, by
`test_protocol_relative_urls_require_positive_url_context_and_leave_code_comments_intact`.
(5) **Unbundled exact-outcome regression corpus (R8-D5, WF3)**:
`tests/unit/test_forge_recording_regression_corpus.py` expanded from 51 to
205 rows (160 unbundled single-probe `PlantedCase` rows each asserting ONE
exact expected outcome — `WRITTEN` with concrete redacted assertions,
`REFUSED` with expected refusal prefix, or `NEEDLE_ERR` with expected error
prefix; 38 `OrdinaryCase` rows asserting `title` whenever set and removing
the 3 rows that pinned `docs.python.org` corruption; 7 `OFFLINE_CORPUS`
rows), promoting the realistic probes from rounds 5, 6, 7 and 8 — all but
two, `R8-P24` and `R8-Inv1`, which 0.15 wrongly claimed (promoted at 0.16,
which holds 260 rows).
(6) **Mutants & offline 6-position coverage (R8-D6, WF1, WF4)**: added
`test_round8_guard_invariants_kill_surviving_mutants` (killing `V8-06`,
`V8-08`, `V8-09`, `V8-10`, `V8-11`, `V8-14`, `V8-17`) and
`test_offline_transform_redacts_all_six_positions_and_non_json_project_ids`
(plus string-list delegation in `scripts/redact_forge_fixtures.py:_remap_projects`,
killing `OFF-01`–`OFF-04` and `R9-D2c`). Full 35-mutant proof in a detached
worktree (`tmp/r8/mutate.log`, GREEN baseline `408 passed`): **35/35 killed,
0 survived** (11 round-7 + 7 round-8 + 5 offline + 12 round-9).
(7) **Wrong facts 2 & 5**: removed the stale `_KNOWN_HOST_TLDS` and `V6-24`
claims from `## Tests` (WF2) and updated `docs/security_posture.md`
(commit `8735a42`, WF5) to state the 0.15 registrable-suffix public-host
rule, API-scoped project-id rule, unified userinfo rule and URL termination
rules.
(8) **Rebase**: rebased onto `origin/develop` `8ee2c38` (`build(ci): pin ruff
and spawn the toolchain through the interpreter (#318)`); verified `.venv`
pins `ruff 0.15.4` via `python -P -m ruff`.

Amendment (0.16, 2026-11-20, the ninth verifier's NO-GO on `f82aa41`, every
defect class fixed by construction in one commit per class, based on
`origin/develop` `8ee2c38`, which had not moved: nothing to merge). The root cause was the
same for the ninth time: a hand-written regex accepting a shape different
from the class it names. Each class below is now built from an explicit
set or a positive rule and proved by a test that enumerates its inputs:
(1) **Userinfo class (R9-D1, WF1, WF4)**: `_RFC3986_USERINFO_CHARS`
concatenated with `@` formed the ASCII range `:-@`, dropping `-` and
admitting `<`, `>` and `?`. Every character class of `recording.py` is
now built by `_char_class` (one `re.escape` per character, optional
negation) from an explicit set; the userinfo class is exactly
`unreserved / pct-encoded / sub-delims / ":"`. Pinned by
`test_regex_character_classes_and_rfc3986_userinfo_enumeration` (all 128
ASCII characters plus one non-ASCII one) and
`test_userinfo_chains_host_token_boundary_and_nested_parens_by_construction`.
(2) **SCP remotes in unified diffs (R9-D2, V9-16)**: `_SCP_IN_TEXT_RE` now
accepts a line-leading `+` / `-` diff marker (`_DIFF_MARKER_CLASS`) and a
`+` inside the user. Pinned by
`test_scp_remotes_in_unified_diffs_and_plus_usernames_are_redacted`.
(3) **Protocol-relative URLs versus `//` comments (R9-D3)**: replaced the
"what comes before the slashes" heuristic with a positive URL-context
rule (whole JSON or decoded structured string value, `="` / `='` / `=`,
`url(`, `](`, `<`); `//` elsewhere starts a comment. Residual, named: a
bare `//host/path` at the start of a line in prose is not redacted (it
is still a needle when the host is seen in any other position, so the
closure oracle refuses it). Pinned by
`test_protocol_relative_urls_require_positive_url_context_and_leave_code_comments_intact`
and `test_protocol_relative_body_context_and_value_rules_by_construction`.
(4) **`glab api` command lines (R9-D4, V9-07, WF3)**: tokenised with
`shlex` (POSIX), value flags skipped through `_GLAB_API_VALUE_FLAGS` in both
the `-H v` and `--header=v` forms, the first positional token is the
endpoint, one function (`_glab_api_endpoint`) serves text and argv; an
unbalanced quote falls back to every `projects/<digits>` after `glab api`.
Pinned by `test_glab_api_shlex_parsing_quoted_flags_argv_and_unbalanced_fallback`
and `test_glab_api_command_recognition_flag_table_and_span_rules_by_construction`.
(5) **Web `projects/<id>/merge_requests/42` (Pref 2)**: redacted without a
`NeedleError`, pinned by `test_web_projects_numeric_path_redacts_without_needle_error`
and `test_web_projects_identity_distinguishes_numeric_ids_from_names_by_construction`.
(6) **Mutants (R9-D5, WF2, WF3)**: `V9-01` (trailing dot of a public host),
`V9-07`, `V9-15` and `V9-16` now die, each by the test named in this
amendment, never by the test 0.15 named (which stayed green under `V9-01`
and `V9-07`). Mutation proof (`tmp/r9/mutate.py`, six throwaway worktrees at the final code, baseline GREEN `472 passed`, strict kill = rc 1 plus a `FAILED` line, log `tmp/r9/mutate.log`): the 35 mutants of `tmp/r8/mutate.log` (11 + 7 + 5 + 12), the 19 verifier mutants `V9-01`–`V9-19` and 180 new `N10-xx` mutants on every function changed this round make 234 mutants: **219 killed, 0 survived, 0 crashed, 0 not applied, 15 retired** because the code they mutated was proven equivalent and removed (`V9-09`, `V9-19`, `N10-29`, `N10-46`, `N10-65`, `N10-67`, `N10-68`, `N10-89`, `N10-97`, `N10-98`, `N10-105`, `N10-112`, `N10-122`, `N10-128`, `N10-166`); no mutant was rewritten to make it die. `V9-05` (`_strip_balanced_tail`'s balance check, unreachable through the public callers because every URL regex only emits balanced trailing parentheses) is killed by a direct test of the function instead of being waived.
Simplifications that the mutation run proved equivalent were made in the
code instead of waived: the dead `=` branch, the `--` and `-` special
cases of `_glab_api_endpoint`, `_GLAB_API_RAW_ENDPOINT_ID_RE` (subsumed by
the fallback search), the `tail_offset` of the fallback scan, the
unreachable kept-span check on `glab` spans, `count=1`, the `(?!/)` and
`///` guards, the tail adjustment of `url_end`, the repetition of
`_USERINFO_AHEAD_RE` and the empty-path guard of `is_structured_json_path`.
Dropping the `"://" in core` guard of `_split_url_tail` also fixed a real
defect the mutant exposed: `//host/ns/repo#__init__` left a stray `__`.
(7) **Corpus (WF5)**: `R8-P24` and `R8-Inv1` and every round-9 planted
survivor and corruption are one exact-outcome row each (260 rows).
(8) **Wrong facts corrected**: 0.15's "every RFC 3986 userinfo character"
was false (WF1); `test_public_reference_hosts_match_by_suffix_and_balance_url_parens`
and `test_api_project_ids_ignore_year_paths_and_support_api_versions` did
not pin the trailing-dot and `["glab", "api", …]` claims (WF2, WF3); one
shared userinfo rule did not cover diff SCP lines (WF4); the corpus
completeness claim omitted two probes (WF5). Each claim now names the test
that turns red, taken from `tmp/r9/mutate.log`. The measurements are written under AC1 and AC3.

Amendment (0.17, 2026-11-22, owner decision after the tenth verifier's
NO-GO on `95ccaf9`; the design, not another round of fixes). Ten rounds
did not converge: each fixed the exact shapes the verifier found, the next
verifier invented new shapes (`src = "//…"`, YAML single quotes, Go and
Rust strings, `${CI_JOB_TOKEN}` userinfo, `insteadOf` blocks, a `glab api`
line with an apostrophe) and found two to five more leaks, and round 10
regressed a 0.15 fix (R10-D1: 13 protocol-relative shapes replaced at
0.15, leaked at 0.16). The cause is the design: the safety of the fixture
rested on recognising every syntactic shape a private value can take in
free text, and that set has no end. 0.17 settles it with one design, one
implementation pass and one verifier:
(a) **Known private values become exact tokens everywhere** (D7). Every
private value the recording holds in a structured field — host, namespace,
repository, logins, numeric ids, node ids, names, emails — and every
deny-list entry is a known value; each is replaced in every string of the
fixture, at every position, by its class token, with one token boundary.
Shapes no longer matter for the guarantee: `const CDN = '//<host>/<ns>/<repo>/'`
is redacted because `<host>`, `<ns>` and `<repo>` are known, not because a
regex recognised a JavaScript assignment.
(b) **Shape recognisers are convenience only** (D1b). `redact_url`,
`redact_urls_in_text`, the SCP normaliser and the noreply rules keep
producing the nice placeholders (`https://forge.example.com/org/project`);
nothing about safety depends on them any more, and the ones whose only job
was safety are deleted (the protocol-relative grammar, the `glab api`
tokeniser, `unredacted_host`, `locations`, the generic `mailbox` and
`public_ipv4` classes: the list is in **What 0.17 removes or demotes**).
(c) **The closure oracle is a separate, deliberately simple module that
refuses** (D3, `src/repoach/forge/closure.py`). It shares no grammar, no
regex and no code object with the redactor — a test imports both modules
and asserts it — and refuses the recording when any known value survives
(casefolded, token-bounded, in the raw text and in its decoded forms), when
any token is host-shaped (D8) and is neither a placeholder, nor on the
public allowlist (option A, registrable suffix), nor on the explicit
non-host list, or when any `user@host` is neither a placeholder, nor
`git@`, nor a declared literal.
(d) **Where the trade-off lands.** An unrecognised shape now yields a
refusal with a named reason and a position, never a leak. Over-refusal on
unusual text is accepted when it is documented (NG4); over-refusal on
ordinary text is not, and the acceptance corpus pins it: 149 ordinary rows
written unchanged or with exactly the stated public-host-preserving output,
across Go, JS, TS, C, C++, Rust, Java and Python with `//` comments and
dotted identifiers, macOS bundle paths, reverse-DNS package names,
three-label file names, CI logs, Markdown and configuration formats.
**Host-shaped, by construction** (D8, amended 0.18): a dotted label run is a
host when it is led by a scheme or by `<user>@` (any length), or when it is
led by `//` or followed by `/<segment>` or `:<port>` and has at least three
labels, a last label in the explicit `HOST_TLDS` list and a first label that
is not a reverse-DNS head; an IPv4 address follows the same rule (0.19): a host when it is led or
followed, a public one then refused, and ordinary text when it is bare. A dotted
name that is neither led nor followed (`response.data.id`, `Xcode.app`,
`config.dev`, `ssl.ca`, `stream.io`, `torch.net`, `os.path`, `self.config`,
`np.array`, `file.py`, `README.md`, `Host mirror.zqcorp.net is down`) is
ordinary text by construction: at 0.17 this paragraph held for two labels
only and was false for three (19 of 70 ordinary snippets corrupted), a
two-label private apex host cited only un-led or protocol-relative is the
named residual (NG4-3) and a third party's three-label private host cited
bare is NG4-8.
**The acceptance corpus, written first and frozen** (`tests/unit/test_forge_recording_acceptance_corpus.py`,
569 rows, one input and exactly one outcome each, through the public
`record` path live and `redact_fixture` offline): every row of the 0.16
regression corpus (260, the seven offline rows as live rows), every planted survivor, refusal and corruption of
round 10 (`tmp/verify10/`, 139 rows: the 34 + 42 fresh samples, the 25
`=`/quote/space shapes and 5 same-host variants, the `.git/config`,
apostrophe, `%2F` and `insteadOf` follow-ups, the item-1 anomalies), and
170 new rows along the axes. Measured on `95ccaf9`: 188 of 569 rows fail
live today; the offline path fails on every row (its 0.17 signature does
not exist yet); the module is `xfail(strict=True)` until the implementer
removes the mark. Values are synthetic; no real host, login, id or deny
entry is in the file.
**Kept from 0.16**: the classes and placeholders (D1, D1c, D2), option A
(the registrable-suffix allowlist, extended by six documentation and CDN
suffixes the round-10 sweep named: `pythonhosted.org`, `cloudflare.com`,
`jsdelivr.net`, `unpkg.com`, `renovatebot.com`, `mend.io`), the NG4
residuals under the accidental threat model, the offline script parity
(D4, now with the remote and the deny-list as inputs), the deny-list
opacity (D3), the ids as a sequence (D1c), D5 and D6. **Superseded**: the
0.16 grammar of positive URL contexts (R9-D3, NG4-5), the shlex `glab api`
rule (R9-D4), the `known_hosts`-only schemeless rule (0.14 Pref 4, NG4-3
narrowed), the generic `mailbox` and `public_ipv4` classes of D3 (now
oracle rules with their own reasons), the kept-host and four-digit
exceptions of the project-id rule (one rule, D9), the 0.9 rule that a
known id standing in free text is refused (it is replaced by its synthetic
number, D7), and every 0.11–0.16 claim that the oracle's independence
rests on `locations()` (deleted).
**Wrong facts corrected** (round 10): L497/L534–556/NG4-5 said "after `="`
/ `='` / `=`" while the code required no whitespace (WF1, now moot: the
grammar is gone); L1078 claimed the oracle shared no recogniser while
`_is_slash_led` and `_PROTO_REL_IN_TEXT_RE` were one grammar (WF2, now a
tested property); L538 "every character class is built by `_char_class`"
held for the userinfo class only (WF3, the word is withdrawn); L554 said a
prose `//host` is refused when in fact it was replaced and written (WF4,
now D7 by construction); L562/L842's unbalanced-quote fallback held only
when the lexer reached the quote first (WF5, the tokeniser is deleted);
`N10-68` changed behaviour and was not an equivalence (WF6); `ftp`/`ftps`
was pinned by no test (WF7, pinned by the acceptance rows
`g_*_https_prose` through `redact_url`'s scheme set). One more fact the
acceptance harness measured: `record()` runs `find_secrets` on the redacted
exchanges, and `SECRET_VALUE_PATTERNS` holds a generic mailbox pattern, so
today's recorder refuses every fixture holding `git@forge.example.com:…`,
`noreply@forge.example.com`, `author@users.noreply.forge.example.com` or
`support@example.com` with `refusing to write a fixture carrying 1
secret-shaped value(s)` — 79 corpus rows, none of which the 0.16 regression
corpus could see because it bypassed `record()`. D3 states the fix: the
recorder's secret gate judges forge-token shapes only; mailboxes are the
oracle's.

Amendment (0.18, 2026-11-22, owner decisions after the eleventh
verifier's NO-GO on `db92cee`: the 0.17 design stands, one rule of it was
too wide; one implementation pass). The verifier measured, at 0.17, 19 of
70 ordinary snippets corrupted (`response.data.id`, `self.logger.info(`,
`request.user.email`, `cfg.Server.Host` rewritten to `forge.example.com`:
55 entries of `HOST_TLDS` are common identifier tails, so any dotted
chain of three names ending in one was a host), 5 leaks through encoded
forms, one crash, one order-dependent test and two over-refusals of
four-part versions. 0.18 fixes each by the rule of its class:
(a) **A dotted name is a host only when it is led or followed** (D8, one
place, `closure.hosts`): led by a scheme `://`, by `<local>@` (a run of
two labels or more) or by `//`, or followed by `/<segment>` or
`:<port digits>` (a run of three labels or more on a listed TLD, first
label not a reverse-DNS head); anything else is ordinary text, whatever
its last label. Known values (D7) are replaced everywhere with no lead
and no follow. `NON_HOST_TOKENS` is deleted: its one entry was a crutch
the rule no longer needs. The new residual is NG4-8: a third party's
private host cited bare in prose is written. Pinned by
`test_a_dotted_name_is_a_host_only_when_led_or_followed_by_construction`,
`test_a_bare_dotted_host_in_prose_is_the_documented_residual` and the
corpus rows `v17_*` (70 ordinary: 0 corrupted, 0 refused but the two
documented placeholder-host rewrites).
(b) **Four-part versions** (`1.8.0.252`, `4.8.1.0`, `pkg == 1.2.3.4`): a
comparator may stand before the quad with spaces, and a quad with a zero
octet reads as a version, so it is never judged as a bare public
address (a led one is still a host); NG4-9 names the address this lets
through. **IPv6**: a bracketed literal is judged like IPv4 (led by a
scheme, `<local>@` or `//` it is a host, un-led and global with more than
two colons it is a `public address`; `[::1]`, `2001:db8::/32` and
`3fff::/20` are placeholder-class, so `http://[2001:db8::1]/<ns>/<repo>`
keeps its host and replaces the namespace and the repository).
(c) **Encoded forms** (D2): `hosts`, `mailboxes` and `public_addresses`
run over `decoded_forms(text)` in `judge`, and `hosts` does in D7 source 3,
so `https%3A%2F%2Fcode.kvncloud.io%2F<ns>%2F<repo>`, `<local>&#64;<host>`
and `<local>%40<host>` are replaced or refused; a percent escape is never
part of a label run, so `%2F%2Fgit.zqcorp.io` leaves no stray `%`.
(d) **Defects**: `_legacy_number` never raises (a run ending in `=` was
padded twice: D3; `test_the_legacy_node_reader_never_raises_on_a_malformed_run`);
the order-dependent test failed because `configure_logging` leaves an
INFO-level filtering wrapper class in structlog's global configuration
and a `debug` event never reaches `capture_logs` (the autouse fixture of
`tests/conftest.py` now resets the wrapper class beside the cache flag,
`test_6_capture_logs_sees_a_debug_event_after_the_level_filter`); a git
author or committer object (a `name` beside an `email`, no `login`)
yields its name words (D5, D7 source 1); the GHE `url` key
`…/api/v3/repos/<o>/<r>/…` no longer raises `NeedleError`
(`path_identity` skips a leading `api/v<N>` before `repos`, `users`,
`orgs` and `apps`); `@font-face` and every CSS, Sass and Tailwind at-rule
is no mention (`CSS_AT_RULES`); the offline transform reads the logins
under keys it drops through `legacy_roles(data, also=raw_logins(exchanges))`,
as the live recorder does; the `_version_context` guard of a led IPv4
address has a test; `uk`, `eu`, `su` and `ac` join the country codes of
`HOST_TLDS` (`ac` stays in `EXTENSION_TLDS`, hence out).
(e) **The harness and seven rows** (owner authorisation): `_live_only_values`
of the acceptance corpus reads every login under a `login` or `username`
key, each word of a name, a mailbox with its local part and host, and
every `id` with its node forms, wherever the object stands, so the five
rows that cite them pass offline with no edit; seven rows are corrected
to the D1b/D7 expectation (`reg_r10_prose_double_slash_host_known_from_remote_is_replaced_as_a_token`,
`r10_p22`, `r10_p23`, `r10_p25`, `r10_p26`, `r10_p27`: a
protocol-relative path is kept; and `code_three_label_private_host_schemeless_no_path`:
NG4-8); 155 rows `v17_*` are added from the verifier's probes (85 planted,
70 ordinary). Corpus at 0.18: 724 rows = 505 planted + 219
ordinary = 658 written + 66 refused.
(f) **Wrong facts corrected**: AC5's counts (the suite gave 4703 passed and
2 failed, not 4704 and 1: one of the two was the order-dependent test);
D4's list of the values only the live path learns (it named the user's
login, name and id, and missed the logins under `assignees` and
`mergedBy`, each word of a name, the local part and host of a mailbox
and repository ids); the claim that ordinary text is safe by
construction (preamble (d) and D8: false for any three-label chain on a
listed TLD at 0.17); the end state, which said no string holds a host
that is not a placeholder (false for an encoded form at 0.17); AC3's
log path (`tmp/impl017/mutate.log`, not `tmp/r11/mutate.log`);
`owns.code`, which still listed the deleted
`tests/unit/test_forge_recording_regression_corpus.py`; D8's `HOST_TLDS`,
which claimed every ISO 3166-1 alpha-2 code and missed `uk`, `eu`, `su`.

Amendment (0.19, 2026-11-22, owner decision after the twelfth verifier's
NO-GO on `8073555`: one blocking item, the zero-octet exception of NG4-9,
no owner decision covered it; one implementation pass). At 0.18 a public
IPv4 address with no zero octet cited bare, a four-part version
(`3.4.0.12`, `Bump to 1.2.3.4`, `pkg = 1.2.3.4`, `ASA 9.12.4.1`) and a real
public DNS address (`resolver 1.1.1.1`) were refused as a `public
address`, and `104.16.0.1:8080` and `104.16.0.1/kvnteam` were written. 0.19
fixes it by the rule of its class, not by an octet test:
(a) **IPv4 follows the host rule** (D8, one place, `closure.hosts`): a
dotted quad is an address only when it is **led** (a scheme `://`, `//` or
`<local>@`) or **followed** (`:<port digits>` or `/<segment>`), in the same
way as a host. A led or followed public address is refused (`public
address`; led and replaced by the recorder as a host first, since 0.17); a
private or reserved one is judged as before; the namespace and repository
after it are read in every case. A bare quad in prose is ordinary text,
whatever its octets (versions, `resolver 1.1.1.1`). The zero-octet
exception (`_version_shaped`) and NG4-9 as worded at 0.18 are deleted with
its assertions; `public_addresses` reads IPv6 literals only; the version
guard `_version_context` stays, for a led or followed quad (H07). The new
residual is NG4-9: a public IPv4 address cited bare in prose is written
(`ping 104.16.0.1`), pinned by `test_a_bare_dotted_quad_in_prose_is_ordinary_text_whatever_its_octets`
and the rows `g_ipv4_public_bare_prose_written_NG4_9`,
`v18_bare_public_ipv4_in_prose_NG4_9`; `104.16.0.1:8080` and
`104.16.0.1/kvnteam` are refused (`v18_followed_public_ipv4_*`).
(b) **IPv6**: a `%<zone>` or `%25<zone>` after the groups belongs to a
bracketed literal (`http://[2a00:1450::1%25eth0]/<ns>/<repo>` is a host and
its namespace and repository are read), and a dotted IPv4 tail is two
groups (`[::ffff:51.15.20.30]`, which the bare-quad rule no longer
catches). An **unbracketed** IPv6 address (`ping 2a00:1450:4001:80b::200e`)
and a bracketed literal with a path but no lead (`[fd00::1]/<ns>/<repo>`)
are named in NG4-10 and in the posture, each with a test
(`test_a_bare_or_un_led_ipv6_address_is_the_documented_residual`).
(c) **Preferences**: `apache.org` joins the public allowlist (25
suffixes: `v17_ci_log_maven` expects the Maven Central URL unchanged);
the blanking of the literals before `judge` decodes is pinned
(`test_the_judge_blanks_the_literals_before_it_decodes`: `x@docs.github.com`
and `x@docs.gitlab.com` are written); a GitLab commit object's
`author_name` and `committer_name` beside `author_email` and
`committer_email` are learnt like a `name` beside an `email` (D5, D7
source 1, `test_known_values_read_a_gitlab_commit_objects_author_and_committer_names`);
a user with a `name` only, and the keys `LOGIN`, `user_login` and
`author_login`, are named in NG4-1 as unreached key shapes (rows
`v18_name_only_user_NG4_1`, `v18_login_upper_key_NG4_1`).
(d) **Corpus**: `Row` gets a `residual` flag, set on the four NG4 rows
that had no marker (`code_three_label_private_host_schemeless_no_path`,
`code_two_label_apex_host_schemeless_prose_residual`,
`v17_apex_two_label_unled_NG4_3`, `v17_zero_width_split_NG4_4`) and on the
new residual rows, and a test computes the planted and ordinary split from
the file (`test_the_planted_and_ordinary_split_is_computed_from_the_rows`),
so the split is no longer written down by hand. Three rows move by the
owner decision: `g_ipv4_public_bare_prose_refused` is written
(`..._written_NG4_9`), `v17_private_ipv4_path_NG4_2` reads its namespace and
repository (`..._followed_reads_namespace`), `v17_ci_log_maven` keeps its
URL; 28 rows `v18_*` are added from the verifier's probes and the new
shapes. The harness reads `author_name`, `committer_name` and the two
mailbox keys among the values only the live path learns.
(e) **Wrong facts corrected**: AC4's C1 count was 261 and the suite
measured 263 passed at `8073555`; AC1 said "the seven the owner authorised",
but the owner authorised six rows (amendment 0.18 (e): the six named there)
and the seventh, `code_three_label_private_host_schemeless_no_path`, follows
from D1, so AC1 now says six plus one forced by D1; the tests table cited
`NON_HOST_TOKENS`, deleted at 0.18, and "H2 two versus three labels" for
`test_host_rule_tld_lists_reverse_dns_heads_and_extension_codes_by_enumeration`;
NG4 ran 1, 2, 3, 8, 9, 4, 5, 6, 7 and is renumbered in order, 1 to 10; and
three guarantees were pinned by tests the table never named, now listed:
the GHE `/api/v3/repos/` path
(`test_path_identity_names_the_namespace_and_the_repository_by_enumeration`),
D4's keyword arguments walked and a positional login refused
(`test_offline_roles_walk_the_keyword_arguments_and_refuse_positional_logins`),
and the URL rewrite of arguments, keyword arguments and results
(`test_offline_redaction_rewrites_urls_in_arguments_keyword_arguments_and_results`).
Corpus at 0.19: 752 rows = 523 planted (a marker, a refusal or a
residual) + 229 ordinary = 684 written + 68 refused, of which 10
residuals.

Amendment (0.20, 2026-11-23, one Fable agent: a full audit of every rule
and reader, then the implementation, after the owner's remark that the
rounds did not converge — each of 0.17, 0.18 and 0.19 fixed a rule on
the examples a verifier had found and not on its class. The audit is
`tmp/fable020/AUDIT.md`; its instrument is a class sweep,
`tests/unit/test_forge_recorder_sweep.py`: every lead, token kind, follow,
encoding and position of every rule, 46 realistic carriers in every host
class and 40 ordinary lines, 4 088 cells through `record()` live and the
offline transform, each cell's expectation computed from the rule set
below and never from the code. At head `2322070` plus the stopped
implementer's edits the sweep failed 215 cells in fifteen classes, the
corpus 3 rows and the crash probe 1 input.) 0.20 fixes each class by a
rule stated by construction, one commit each:
(a) **A path is read only after a led host** (D7 source 3, C1; the
orchestrator's correction of the 0.19 rule): a followed address is judged
but its path is never read, so `curl 10.0.0.12:8080/metrics` is written
unchanged (NG4-2); rows `v17_private_ipv4_path_followed_NG4_2` and
`v18_followed_private_ipv4_port_path_NG4_2` flip back.
(b) **A lone path segment is a page, not a namespace** (D7 source 3, C2,
NG4-11): `path_identity` names a lone segment a namespace, so source 3
learnt `docs`, `health`, `metrics`, `explore` from any URL on a
third-party host and `replace_known` rewrote that word everywhere else in
the fixture; a text path now yields values only with both a namespace and
a repository or through an API root (`users/`, `orgs/`, `repos/`, `apps/`,
`api/vN/`). Row `v17_prefixed_label_known_host` is corrected under this
rule (`assets` is a page).
(c) **Nothing is read after a placeholder-class or public host**, IPv4,
IPv6 and name alike (D7 source 3, C3, NG4-12): the 0.18 read after an
allowed IPv6 literal corrupted `http://[2001:db8::1]:8080/docs` and would
have learnt `api` and `users` from `http://localhost:3000/api/users`; row
`v17_ipv6_url_private_path` flips to `v17_ipv6_doc_url_path_NG4_12`.
(d) **A quad after a single `/` is a version context**, and a version
label allows a run of separators (D8 IPv4, C4): `/ojdbc8/19.3.0.0/`,
`/x/1.2.3.4/files`, `Version: 1.8.0.252/linux` and `version = 1.8.0.252:8443`
are written; `1.2.3.4/24` is still an address; the comparator allowance
with spaces is pinned (the verifier's hand mutant C59).
(e) **`0.0.0.0`, `255.255.255.255` and `::` are placeholder-class** (D8,
C5): `Uvicorn running on http://0.0.0.0:8000` is unchanged.
(f) **The oracle never raises** (D3, C6): `_ipv6` builds the address from
the canonical dotted tail (`[::ffff:01.2.3.4]`), `node_id_numbers` reads at
most twenty digits (`U_` + 5 000 digits raised `ValueError`); every other
call that can raise on input is audited in `AUDIT.md` section 5.
(g) **`\/` and `\xHH` are a decoded layer** (D3, D2, C7): the
string-escape layer is `\uXXXX`, `\xHH`, `\/`, so
`http:\/\/<host>\/<ns>` is judged and learnt as its plain form (59 sweep
cells leaked; probe E06 was filed under NG4-4, which only excludes escapes
beyond four rounds).
(h) **An IPv6 URL in text is a text URL** (D1b, C8): `[` was a body
delimiter, so only the token replacement rewrote
`http://[2a00::1]:8443/ns/repo/x`, keeping the port and the path, and the
offline transform rewrote it again (48 cells not a fixed point; the
verifier's port inconsistency). One rule: a rewritten URL carries the
placeholder host and no port; a kept one is byte-identical but for its
scheme and host case.
(i) **A mailbox on a placeholder-class address names no one** (D8, C9):
`root@127.0.0.1`, `deploy@0.0.0.0`, `admin@203.0.113.9` were refused (45
cells); a credential on one is still refused; `forge.example.com` stays
out (NG4-5).
(j) **Language keywords after `@` are no mention** (D2, C10, C11):
`CSS_AT_RULES` is completed from the CSS, PostCSS and Tailwind
specifications, `DOC_TAGS` (JSDoc, TSDoc, Javadoc, PHPDoc, Doxygen) and
`LANGUAGE_ANNOTATIONS` (Java, Kotlin, Swift, Dart and their standard
libraries) are added, `PYTHON_DECORATORS` completed; their union
`LANGUAGE_AT_WORDS` is what `mentions` skips (48 cells were `@author-2`).
The userinfo characters `:${}` block a mention like a letter does
(`${TOKEN}@localhost` was `@author-2`). A framework's annotation
(`@Autowired`, `@Test`) still gets a class word: named in D2.
(k) **A short mention-only handle never refuses the recording** (D2, D7,
C12): `cc @bob` or `@var` raised `NeedleError`; a handle shorter than
`NEEDLE_MIN_LENGTH` is replaced in its `@` form and is no value (NG4-1).
(l) **`EXTENSION_TLDS` states its class and is completed** (D8, C13):
source, build and binary file extensions a tool cites as `<file>:<line>`;
`cr`, `cu`, `gd`, `mm`, `pro`, `sc`, `st`, `sv` join (`kernel/foo.bar.cu:12:3`
was an unknown host).
(m) **`nuget.org` joins the allowlist** (D8, C14; 26 suffixes).
(n) **The readers are linear** (C15): `_led`, `_version_context`, `hosts`
and `public_addresses` copied `text[:start]` per run (the oracle alone,
`closure.judge` on an empty `Known`: 16 s for 5 000 URLs, 15 s for a 1.2 MB
diff; now 0.15 s and 1.3 s — not `record()`, whose cost is `residual`'s,
see 0.21 (e)).
(o) **The harness** (T01): the split test asserts the five counts computed
from the file and that every residual is planted.
(p) **Wrong facts corrected**: D8 IPv4 "its namespace and repository are
read in every case" and the examples `127.0.0.1:8000`, `203.0.113.9`
"followed, replaced" (false: a placeholder is skipped before any read, a
followed address has no path read); D8 IPv6 "read even when the literal is
allowed" (reversed by (c)); the placeholder-class list, the allowlist
count, the mailbox literals, the decoding layers, NG4-2's second sentence,
NG4-9's "in prose", the row name `v18_v13_ipv6_unled_ula_path_NG4_2`
(`…_NG4_10`); NG4-11 and NG4-12 added; every guarantee of 0.20 named with
its test in the tests table. Corpus at 0.20: 800 rows = 545 planted + 255
ordinary = 729 written + 71 refused, 16 residuals; 48 rows `v20_` (22
planted, 26 ordinary, 3 residuals — this line said 4; the 0.20 verifier
counted the flag).

Amendment (0.21, 2026-11-23, one Fable agent, after the 0.20 verifier's
NO-GO and the owner's scope change: the red points only, every green rule
untouched; the diagnosis is `tmp/fable021/DIAGNOSIS.md`, the untouched
preferences `tmp/fable021/DEFERRED.md`). Each of the three blocking items
was a missing **generator dimension** of the 0.20 sweep, not a missing
example, and each gets its dimension (1 060 cells join the 4 088):
(a) **Everything before `/-/` is an identity** (D1b, D7 source 3, B1):
`path_identity` read the GitLab separator only with two or more segments
before it, so `https://<host>/<ns>/-/issues` kept `<ns>` on every host
class, live and offline, and `known_values` skipped a text path with no
repository unless it went through an API root. Now one segment before
`-` is the namespace (`<group>/-/issues`, `<user>/-/snippets`), two or
more are the namespace and the repository, and after an owner root every
segment up to the separator is the namespace (`groups/<g>/<sub>/-/epics`,
`users/<u>/-/snippets`); `groups` joins the owner roots and the API roots;
`_api_route` counts a `-` after a first segment as naming an owner. The
placeholder is `org/-/<route>`. Three assertions that enshrined
`foo/-/jobs/5` → `foo` kept are corrected under the verifier's proof.
Dimension: `route_cells`, 17 forge route grammars (GitLab `/-/`, `groups`,
`users`; GitHub `orgs`, `users`, `repos`; Gitea `org` and its API;
Bitbucket's repository route) × 5 host classes × 2 schemes, the identity
planted and the route words held unreplaced (157 cells). The path after a
public forge host is still not read (D7): `https://gitlab.com/<g>/-/x` is
rewritten by the convenience layer alone.
(b) **A mention-only handle equal to a language word is the named residual
NG4-13** (D2, B2, decided under the owner's goal): `cc @test`, `@main`,
`@link` of a non-participant whose login is one of the 425
`LANGUAGE_AT_WORDS` is written in its `@` form, since the finder cannot
tell the handle from the word by construction; narrowing the skip set to a
syntactic context (doc tags in a comment, annotations line-leading) would
rewrite 33 ordinary mention cells of the sweep (`cc @param please`,
`(@Override)`) and pasted Swift or Java (`completion: @escaping`) to
`@author-n`, and would not close a line-leading `@test please review`, the
commonest mention shape; where the goals conflict for one shape the
owner's tie-break is ordinary text with a named residual. A participant's
login equal to a language word is a known value: replaced wherever it
stands (`cc @test please review, test` with `test` the comment's author →
`cc @author please review, author`), refused when it survives. Dimension:
`language_word_cells`, every word mention-only (written) and as a
participant's login (replaced; a word shorter than `NEEDLE_MIN_LENGTH` is
the D7 needle refusal; the one class word is skipped), 849 cells.
(c) **A recording nested past the readers is refused, never crashed on**
(D3, B3): `decode_json_values` caught the decode error only, so a body of
20 000 `[` raised `RecursionError` out of `record()`. Measured after
catching it with the decode error: such a text is text and is written; a
value that **does** decode (depth 990 under Python 3.12, whose C decoder
goes deeper than the Python frame limit) still crashed a recursive reader.
`record()` and `redact_fixture()` now turn a `RecursionError` of any reader
into the named refusal `nesting too deep` (`UnclassifiedValueError` live,
`ResidualValueError` offline), nothing written: the readers' bound is the
interpreter's, and a fixed depth constant would have been a second, false
boundary (string-in-string layers defeat any per-value bound). No
committed forge answer nests past 12. The oracle has no recursion.
Dimension: `nesting_cells`, depth 1 to 20 000 in three shapes and three
positions, shallow cells ordinary, deep ones `BOUNDED` (written or refused
as `nesting too deep`, never a crash), 54 cells.
(d) **The three operator mutants the verifier found alive are pinned**:
M21 (`_BRACKETED` 45 → 39 characters: the 45-character fully expanded
mapped literal is a host, led, and a public address, un-led), R01
(`_path_after` reads the path after a five-digit port) and R13
(`_kept_host` compares the placeholder casefolded; asserted directly,
since `urlsplit` lowercases every host the text rewrite hands it). They
were outside the 0.20 operator classes (a constant mutated by `+1`, a
regex string never, `_kept_host` not in the function list), so the 0.20
line "0 non-equivalent survivors" holds for the sites it ran and not for
these three; corrected in AC3.
(e) **Wrong facts corrected**: NG4-11 said "a third-party host" where the
rule applies to every host (the instance, a public forge, a third party);
amendment (p) said "4 residuals" among the `v20_` rows where 3 carry the
flag; the timings of (n) and AC8 (5 000 URLs in 0.15 s, a 1.2 MB diff in
1.3 s) were the oracle's alone, not `record()`'s — `record()` on 1 000
URLs on distinct hosts takes 37.7 s, 37.0 s of it in `closure.residual`
(one `token_pattern` compiled per value per string; the `re` cache holds
512), measured by `cProfile` and deferred by the owner's scope; the
mutation claim, above. Left as they were by the owner's scope change and
listed in `tmp/fable021/DEFERRED.md`: the performance of `record()`, the
`home_path` refusal of CI runner homes, the scp branch of NG4-11, the
stdin parity case, Bitbucket's `workspace` route and 2.0 API, the route
words on the recorded instance. Corpus at 0.21: 815 rows = 558 planted +
257 ordinary = 742 written + 73 refused, 18 residuals; 15 rows `v21_` (13
planted, 2 ordinary, 2 residuals).

Amendment (0.22, 2026-11-23, one Fable agent, after the 0.21 verifier's
NO-GO and the owner's scope: the red points only, every green rule
untouched, the preferences of `tmp/fable021/DEFERRED.md` still deferred).
The four red points are one class — a forge route or a package-registry
grammar whose identity sits elsewhere than the generic `<ns>/<repo>`
reading assumes — and get one table-driven rule in `path_identity`:
**reserved route words are never an identity, and identity slots are
given per grammar** (D1b, D7 source 3). Run against the 0.21 code the
new sweep dimension finds 594 failing cells in the four classes (46
leaks, 548 corruptions, `tmp/fable022/sweep_base.log`); at 0.22, 0.
(a) **A reserved route word is never an identity** (the corruption
`uploads` → `org`). `_RESERVED_ROUTE_WORDS` holds GitLab's top-level
words — the owner's list: `uploads`, `help`, `explore`, `admin`,
`dashboard`, `snippets`, `search`, `public`, `s`, `files`, `assets`,
`import`, `oauth`, `jwt`, `.well-known`, `-` — and the words GitHub
(`marketplace`, `settings`, `login`, `notifications`, `topics`, …), Gitea
(`repo`, `milestones`, `attachments`, `avatars`) and Bitbucket
(`account`, `plugins`) reserve and let no user or group take; a path led
by one, in any case, names no one: nothing is learnt and the word is
written as it stands. `user` stays out: `<host>/user/<repo>` is the
stand-in of every documentation example and the regression row
`reg_short_third_party_url_x_com` keeps it rewritten. An identity that
follows a reserved word is not read: NG4-14, below. Named test
`test_a_reserved_route_word_is_never_an_identity`.
(b) **The Web IDE reads its project path** (the leak `<ns>/<repo>` kept
in `/-/ide/project/<ns>/<repo>/edit/<branch>/-/<path>`, every host
class). After `-/ide/project/` the segments up to an IDE word (`edit`,
`blob`, `tree`, `merge_requests`, `_IDE_ROUTES`) or the end are the
namespace and the repository, subgroups included; `_api_route` counts
the route as naming an owner, so a text path with the namespace alone is
learnt. Written `-/ide/project/org/project/edit/…`. Named test
`test_a_web_ide_route_reads_its_project_path`.
(c) **An owner root reads the identity slots of its grammar** (the leaks
`<repo>` in Bitbucket Server's `users/<u>/repos/<repo>/…` and the team
slug in GitHub's `orgs/<o>/teams/<team>`). `_IDENTITY_SLOTS` names, per
root, the route word that opens the next slot and its placeholder:
`users/<u>/repos/<r>` and `projects/<KEY>/repos/<r>` name a repository
(`users/org/repos/project`), `orgs/<o>/teams/<t>` a team, a namespace of
people (`orgs/org/teams/org`), in the `api/vN/` shape too;
`_follow_segments` reads the whole path of a project route so
`known_values` learns what `redact_url` writes. Named test
`test_an_owner_root_reads_the_identity_slots_of_its_grammar`.
(d) **A package tarball before a dash names no one** (the corruption
`lodash` → `org` from `<pkg>/-/<pkg>-<version>.tgz` on a registry host
that is not allowlisted). A segment followed by `/-/<same segment>-
<version>.tgz` is a package, scoped (`@scope/<pkg>/-/<pkg>-<v>.tgz`) or
under a Nexus `repository/<repo>/` prefix, neither a namespace nor a
repository: the host is replaced when it is private, the path is written
as it stands. `yarnpkg.com` and `npmmirror.com` join the allowlist (28
suffixes), so a yarn.lock line is kept whole. Named test
`test_a_package_tarball_before_a_dash_names_no_one`.
(e) **The survivor N14 is pinned**: the owner after an owner root ends
at the FIRST `-`; `groups/<g>/-/epics/-/x` keeps its second dash as a
route. Named test `test_the_owner_root_stops_at_the_first_dash`. Two
mutants of the new code had no assertion telling them from it (a project
route read only from five segments, the `dash < 1` guard of the tarball
check) and are pinned too.
(f) **Dimension added.** `route_cells` gains nine route grammars (the
Web IDE in five shapes, Bitbucket Server's two repository routes,
GitHub's team slug and its API shape) on the five host classes; every
forge's reserved words, from the sweep's own per-forge table and not
from the code under test, in two shapes on the five host classes; the
npm tarball grammar in five shapes (plain, scoped, Nexus prefix,
prerelease, a yarn.lock diff) on the five host classes and five registry
hosts: 1 183 route cells, up from 157; the sweep holds 6 174 cells.
Fourteen `v22_` rows join the corpus (10 planted, 4 ordinary): 829 rows
= 568 planted + 261 ordinary = 756 written + 73 refused, 18 residuals.
(g) **One old probe line changes.** `tmp/verify018/probes.py`
`p02_unknown_host_dot_segment` planted a namespace after `.well-known`
(`<host>/.well-known/<ns>`); under (a) the segment after a reserved word
is not read (RFC 8615 well-known URIs never name a user), so the probe
now reports that cell as a leak: it is NG4-14, not a regression; the
017 and 019 probe sets are identical line for line.

Amendment (0.23, 2026-11-23, one Fable agent, after the 0.22 verifier's
NO-GO and the owner's scope: the two red points only, every green rule
untouched, the verifier's other findings measured and appended to
`tmp/fable021/DEFERRED.md`).
(a) **A remote names a repository by construction** (the regression: a
recorded remote whose namespace is one of the 35 `_RESERVED_ROUTE_WORDS`
— nine are real GitHub accounts, `jwt` and `assets` among them — lost
its namespace and repository as known values, since `path_identity`
applied the reserved-word rule to the remote's path). The rule is a URL
path's. `path_identity(segments, remote=True)` reads a remote as
`<ns>/…/<repo>` (`_repository_path`, the generic reading, a lone segment
the namespace) whatever its first word, and `known_values` passes the
remote through it; the structured URL fields (`web_url`, `avatar_url`)
keep the route grammar, so an avatar under `uploads/-/system/…` still
names no one. Live and offline agree on `snippets/<repo>` (the 0.22
verifier's parity cell), both learning the two values from the remote;
`jwt` and `s` are shorter than `NEEDLE_MIN_LENGTH` and are the D7
configuration error (`needle segment 1: too short`), as any short
structured value since 0.10. The price is that of every known value: the
word is replaced wherever it stands (`the snippets page` → `the org page`
when the remote is `snippets/<repo>`). Named test
`test_a_remote_path_names_a_repository_by_construction`; rows
`v23_remote_namespace_reserved_word_*`; sweep cells `remote:<word>`, one
per reserved word an account may carry.
(b) **The scope of GitLab's npm registry is a root group** (the leak:
`@<scope>` kept on the instance). On `api/v<N>/packages/npm/`,
`api/v<N>/projects/<id>/packages/npm/` and
`api/v<N>/groups/<id>/-/packages/npm/` (`_npm_registry`) a segment
`@<scope>` or `@<scope>%2F<pkg>` (`_NPM_SCOPE_RE`) carries a root group
of the instance by construction — GitLab's registry accepts no other
scope (0.24: this premise is false and the rule is withdrawn, 0.24 (c)) —
so the scope is the namespace, written `@org`, in the tarball
path (`@<s>/<p>/-/<p>-<v>.tgz`), in GitLab's own file name
(`@<s>/<p>/-/@<s>/<p>-<v>.tgz`, which the generic reading corrupted at
0.22 into `org/project/-/org/project-1.0.0.tgz`), in the encoded metadata
path and under `-/package/…/dist-tags`; the package and its files name no
one; what stands before `packages/npm` (`projects/<id>`, `groups/<id>/-`)
reads as at 0.22. Decided under the owner's goal for every other host: a
scoped tarball on a private registry (`npm.<corp>/@<s>/<p>/-/<p>-<v>.tgz`,
a Nexus `repository/<r>/@babel/core/-/…`) stays ordinary text — the
namespace rule there would turn every proxied public scope (`@babel`,
`@types`) into `org`, the corruption the sweep's Nexus cells measure —
and the third party's scope it may carry is the named residual NG4-15.
Named test `test_a_gitlab_npm_scope_is_a_root_group`; rows
`v23_gitlab_npm_scope_*`, `v23_gitlab_npm_unscoped_package_names_no_one`,
`v23_scoped_tarball_on_a_private_registry_keeps_its_scope_NG4_15`,
`v23_scoped_tarball_on_a_nexus_proxy_keeps_its_scope`; sweep grammars
`gitlab_npm_*` (five shapes on the five host classes).
(c) **The roots are matched in any case.** `core[2] in _API_RESOURCES`,
`core[0] == "repos"` and `core[0] in _API_RESOURCES` compared raw while
`_project_root`, the reserved words and `_ide_route` casefold:
`API/V4/Repos/<o>/<r>` read `API`, `V4`, `Repos` and `<o>` as a
four-segment namespace. Casefolded now, the root word written as it
stands. Named test `test_an_owner_root_is_read_in_any_case`; row
`v23_api_resource_root_in_upper_case_reads_its_identity`.
(d) **Facts corrected.** NG4-14 cited the ordinary rows
`v22_*_reserved_word_*` as its rows; it cites the live row
`v22_gitlab_s_redirect_identity_after_a_reserved_word_NG4_14` now (the
instance host replaced, the identity after `s/` written; the `residual`
flag of the corpus asks for a byte-identical text, which no row on a
private host can give, so the row is planted by its host marker and
named for the residual). The sweep's module docstring claimed an
expectation computed "never from the code under test"; its `logins`
dimension enumerates `LANGUAGE_AT_WORDS`, `CLASS_WORD_RE` and
`NEEDLE_MIN_LENGTH` and its string walker decodes with
`decode_json_values`; the docstring says so and names the route tables as
the sweep's own.
(e) **Counts.** Fourteen `v23_` rows (14 planted, 1 of them refused) and
one `v22_` row join the corpus: 844 rows = 583 planted + 261 ordinary =
770 written + 74 refused, 18 residuals. The `routes` dimension holds
1 266 cells (0.22: 1 183). Out of scope by the owner's brief, measured
and appended to `tmp/fable021/DEFERRED.md`: the GitHub identity slots
`orgs/<o>/members/<login>`, `teams/<t>/memberships/<login>`,
`repos/<o>/<r>/collaborators/<login>` and the others (7 cells), Bitbucket
Server's `scm/~<user>/<repo>.git`, GitLab's Maven artifacts, the allowlist
for maven.org, helm.sh, bitnami, rubygems, packagist and anaconda, the
NG4-14 grammars, and the verifier's seven cells that list a package name
both as a marker and as a kept word.

Amendment (0.24, 2026-11-23, one implementer, after the 0.23 verifier's
NO-GO and the owner's scope: « on garde tout le reste qui est vert, on n'y
touche pas, on travaille sur les points rouges »; every green rule
untouched, `tmp/fable021/DEFERRED.md` still deferred).
The class of the three red items is one: since 0.22 the reserved-word rule
(`_RESERVED_ROUTE_WORDS`) applies to a TEXT route path only, never to a
hard source, and 0.23 had exempted the flat remote alone; at 0.21, before
the table existed, every cell below was right.
(a) **A nested remote under a reserved root word is one namespace** (B1-a,
the parity break: a remote `<word>/<sub>/<repo>` learnt both segments, but
a text path led by the word is written as it stands, so the live recorder
wrote `org/org/project` and the offline transform, reading that text
again, `org/project`; 14 cells of the 0.23 verifier). One rule, read the
same way live and offline: a hard identity path — the remote, an argv API
path — yields the namespace every segment but the last and the repository
the last, whatever the words (`path_identity(remote=True)`), and a
namespace of two segments or more is a value as each of its leading paths
too (`<word>/<sub>` → `org`, `known_values`), so the text path the route
grammar keeps is written `org/project` (after a `/-/` route, on the project
and group pages, in a clone address) and is a fixed point. A nested remote
whose words are no reserved ones was already written so by the route
grammar. Named test
`test_every_hard_identity_path_reads_a_repository_path_by_construction`;
`test_a_remote_path_names_a_repository_by_construction` pinned the
defective `org/org/project` and pins `org/project` now; rows
`v24_nested_remote_under_reserved_root_*` (GitLab subgroups under
`marketplace`, `uploads`, `settings`); sweep cells `remote:nested:<word>:*`.
(b) **An argv API path names a repository by construction** (B1-b, the
leak: `repos/<word>/<repo>/…` and `projects/<word>%2F<repo>/…` in argv
went through the route grammar, so a reserved namespace learnt nothing and
the repository was kept; 8 cells of the 0.23 verifier). `known_values`
passes `remote=True` to `_segments` for `_api_project` too. The audit of
every `_segments(..., hard=True)` call site: three. The remote and the argv
API path are identity paths and read by construction; the third, a
`URL_KEYS` value (`web_url`, `avatar_url`), is a URL whose path carries
routes, so it keeps the route grammar (read as a remote, `merge_requests`
and `-` would be learnt as namespaces and `uploads/-/system/…` would name
`uploads`; the 0.23 verifier's mutant N25, every hard source read as a
remote, is killed by that assertion). A short reserved namespace
(`repos/jwt/<r>`, `projects/s%2F<r>`) is the D7 configuration error
`needle segment 1: too short`, as every short structured value. Named test
`test_every_hard_identity_path_reads_a_repository_path_by_construction`;
rows `v24_argv_github_repos_path_with_a_reserved_owner_is_read`,
`v24_argv_gitlab_encoded_project_with_a_reserved_namespace_is_read`; sweep
cells `argv:<shape>:<word>`.
(c) **A scope read from a URL is never learnt** (B2-c, the corruption:
0.23 (b) learnt the `@<scope>` after GitLab's `packages/npm/` as a root
group on the premise that GitLab's registry accepts no other scope. The
premise is false: package forwarding brings `@types` and `@babel` to these
endpoints, and the project and group endpoints impose no scope naming, so
a forwarded `@types` was learnt and written `@org` in the URL and in every
import of the fixture's code). Decided under the owner's goal (prefer
ordinary text): a scope read from a URL is never a known value, on any
registry host, GitLab's own included; a scope that equals an already-known
namespace (the remote's, the recorded group) is replaced, as that known
value, the existing behaviour; every other scope is ordinary text on every
host. What follows `packages/npm/` names no one and is written as it
stands; the prefix before it (`projects/<id>`, `groups/<g>/-`) reads as at
0.22. `_NPM_SCOPE_RE` is deleted. NG4-15 covers every npm registry now,
the instance included, and names the company-scope case. Named test
`test_an_npm_scope_is_never_learnt_on_any_registry` (renamed from
`test_a_gitlab_npm_scope_is_a_root_group`); rows
`v24_forwarded_types_scope_next_to_a_gitlab_npm_url_stays_in_code`,
`v24_forwarded_babel_scope_on_a_gitlab_project_endpoint_stays_in_code`,
`v24_company_scope_on_its_own_registry_is_ordinary_text_NG4_15`,
`v24_gitlab_npm_scope_equal_to_the_remote_namespace_is_replaced`; the five
rows `v23_gitlab_npm_scope_*` pinned the withdrawn rule and are corrected
and renamed `*_is_ordinary_text_NG4_15`, planted by their host marker;
the sweep's five `gitlab_npm_*` shapes keep their scope and two forwarded
public scopes join them.
(d) **N13 is pinned**: a lone remote segment that is a reserved word
(`https://<host>/assets`) is the namespace, the remote reading for every
length (the 0.23 verifier's mutant N13, the lone segment sent to the route
grammar, survived). Named test
`test_every_hard_identity_path_reads_a_repository_path_by_construction`.
(e) **Facts corrected.** 0.23 (a) stated the remote rule as the whole of
B1; the claim of completeness was wrong: the argv API path was a second
hard source the reserved-word rule still reached, and the nested remote
broke the parity gate. 0.23 (b), D7 source 3 and NG4-15 stated the false
premise above; each now says 0.24.
(f) **Counts.** Nine `v24_` rows join the corpus (9 planted, 0 refused,
0 ordinary) and five `v23_` rows are corrected: 853 rows = 592 planted +
261 ordinary = 779 written + 74 refused, 18 residuals. The `routes`
dimension holds 1 484 cells (0.23: 1 266: 99 nested remote cells, 99 argv
cells, 20 forwarded scope cells); the sweep holds 6 475 cells.
Not a defect of this pass, measured and appended to
`tmp/fable021/DEFERRED.md`: the 0.23 verifier's `b2_priv_enc_meta`
(`GET https://npm.<corp>/@types%2Fnode`, a lone segment on a private host
written `org` by the generic reading, the same at 0.21) and
`n_priv_scope_is_participant_login`, a harness case (the cell compares the
offline transform of a raw text that holds a login only the live recorder
can learn; `_offline_parity_possible` excludes it in the corpus, and the
written fixture is a fixed point).

Amendment (0.25, 2026-11-23, one implementer, after the 0.24 verifier's
NO-GO and the owner's scope: « on garde tout le reste qui est vert, on n'y
touche pas, on travaille sur les points rouges »; every green rule
untouched, `tmp/fable021/DEFERRED.md` still deferred).
(a) **An API path is read from the recorded argv only** (the 0.24
regression: 0.24 (b) read `_api_project` with `remote=True` over every
string of `fixture_strings(raw, "")`, not over the argv it names, so a
body or a review comment's `path` led by `repos/<word>/…` or
`projects/<word>%2F…` learnt the word, reserved or not, as a hard
namespace and rewrote it everywhere: `make the page public` beside a path
`repos/public/index.html` was written `make the page org`; 4 corruption
cells of the 0.24 verifier, right at 0.23). The rule: the hard reading of
`_api_project` (`path_identity(remote=True)`) applies to the argv strings
of the recorded calls and to nothing else. `_argv_strings` yields them:
the `args` list of an exchange object (one holding `args` and `stdout`)
wherever it stands in the raw recording, its strings only, never a
string's decoded JSON. Every other string is read by the text readers
alone (`_ENCODED_PROJECT_RE` as a soft source, `closure.hosts`). Measured,
the 0.23 text reading kept for those strings would leave the older variant
without a reserved word corrupted (`repos/frontend/main.py` in a body or a
path field written `repos/org/project`, at every tree since 0.21); dropping
it closes that variant too (its cost, the leaks no cell then exercised, is
0.26 (b)): no verdict of the cells of the 0.20
to 0.24 verifiers changes but the four corruptions this item closes,
`sweep_v2.py` keeps its 17 findings line for line, the corpus and the
sweep stay green; so the variant is closed and pinned, not deferred. The
argv path is still read by construction. Named test
`test_only_an_argv_string_is_read_as_an_api_path` (and
`test_every_hard_identity_path_reads_a_repository_path_by_construction`
unchanged); rows
`v25_review_path_field_led_by_repos_and_a_reserved_word_is_ordinary`,
`v25_review_path_field_led_by_repos_public_is_ordinary`,
`v25_body_led_by_repos_and_a_reserved_word_is_ordinary`,
`v25_body_led_by_an_encoded_project_and_a_reserved_word_is_ordinary`,
`v25_body_led_by_repos_and_a_plain_directory_is_ordinary`,
`v25_review_path_field_led_by_repos_and_a_plain_directory_is_ordinary`,
`v25_argv_repos_path_with_a_reserved_owner_is_still_read_by_construction`.
(b) **A URL field path learns no joined namespace** (the 0.24 verifier's
mutant V09, `if remote:` to `if remote or hard:` in the joined-prefix loop
of `known_values`, survived: nothing pinned that only a path read by
construction makes a namespace of two segments or more a value as a whole
path). A `URL_KEYS` value names its segments one by one, so a GitLab
`web_url` under `kvnteam/kvnsub/…` beside a body `kvnteam/kvnsub-old`
writes `org/org-old`, not `org-old`. Named test
`test_a_url_field_path_learns_no_joined_namespace`; row
`v25_gitlab_url_field_nested_group_learns_no_joined_namespace` (its body
carries the URL too, so the offline half can read what the field names).
(c) **Facts corrected.** 0.24 (b) said `known_values` passes `remote=True`
to `_segments` for `_api_project`, and D7 source 1 and the posture named
"an argv API path": the code read every fixture string that way. D7
source 1 and the posture now say an argv string of a recorded call, and
only there (`docs(security)` commit of 0.25).
(d) **Counts.** Eight `v25_` rows join the corpus (2 planted, 0 refused,
6 ordinary): 861 rows = 594 planted + 267 ordinary = 787 written + 74
refused, 18 residuals; the sweep holds 6 475 cells, unchanged.
Not a defect of this pass, measured and appended to
`tmp/fable021/DEFERRED.md`: a numeric group id learnt as a namespace
(`groups/77/-/packages/npm/…`, 3 cells, and `groups/77123/-/…` beside
`at line 77123` written `at line org`), the `.npmrc` `@types:registry=`
read as a mention (1 cell), the query string of a reserved route URL
dropped (3 cells), and `_offline_parity_possible`, which misses a value
only a URL field names (the 2 harness parity cells of the 0.24 verifier).

Amendment (0.26, 2026-11-24, one implementer, after the 0.25 verifier's
NO-GO; the owner's scope unchanged: only the red items, every green rule
untouched, `tmp/fable021/DEFERRED.md` still deferred).
(a) **The provenance table is THE rule of which source names an
identity.** Six rounds moved one boundary, which strings are read as a
hard identity source and which as text, because the boundary was implied
by which `_segments` call ran on which strings (0.22 to 0.25). It is now
declared once, `recording.PROVENANCE`, an ordered tuple of
`(source, reading)`: `remote` → `repository`, `url_field` → `route`,
`argv` → `repository`, `text` → `text`. `known_values` builds the items of
each source (the remote's path; every `URL_KEYS` string of a decoded
answer; `_api_project` of every `_argv_strings` string; every
`fixture_strings` string, the three sources above included) and runs each
item through the reader its row names, in the table's order; no
`_segments` call stands outside the three readers, and `_api_project`
and `_text_api_projects` run once each, as a source and inside the text
reader. The readings are those of 0.25: `repository` is hard and by
construction (`path_identity(remote=True)`, the joined namespace values),
`route` is hard through the route grammar, reserved words applying and no
joined value (V09), `text` is soft and never refuses. The order is the
learning order: a value two rows name keeps the class of the first, so
a value a URL field names as a namespace and an argv path as a
repository is written `org` (the 0.25 verifier's mutant B19). Named test
`test_the_provenance_table_is_the_one_rule_of_what_names_an_identity`
(the table enumerated; every `_segments` call inside a reader; the
source and reading names of the dispatch equal to the table's);
`test_a_value_a_url_field_names_as_a_namespace_keeps_its_class`; row
`v26_url_field_namespace_keeps_its_class_before_an_argv_repository`,
whose raw-text parity half is declared impossible (`url_field_class`):
the offline transform reads a committed fixture, whose `web_url` is
already written, so it learns the value from the argv alone; this is the
deferred `_offline_parity_possible` class, the fixed-point half holds.
(b) **A GitHub API path in text names a repository through a REST route
word** (the 0.25 regression, leak side: 0.25 dropped every text reading
of `repos/<o>/<r>/…`, so a body, kwargs, positional or nested method
args, stdin, stderr, a stdout field, a review comment's `path`, a raw
stdout or a JSON string inside a body led by such a path learnt nothing;
34 LEAK cells of `tmp/verify025/cells25.py`). In the `text` reading,
`[/][api/v<N>/]repos/<o>/<r>/<word>…` (`api/v3/` on GitHub Enterprise, `api/v1/`
on Gitea), in any case, not inside a longer path or a URL,
names `<o>` as a namespace and `<r>` as a repository, soft, only when
`<word>` is one of the 74 sub-resources of GitHub's REST repository
endpoints (`_GITHUB_REPO_API_ROUTES`, transcribed from the `paths`
`/repos/{owner}/{repo}/<word>` of GitHub's REST OpenAPI description,
`github/rest-api-description`, read 2026-11-23; `pulls`, `issues`,
`hooks`, `branches`, `commits`, `contents`, `git`, `actions`,
`releases`, `collaborators`, …). Every other word after the repository
(`main.py`, `index.html`, `src`) leaves the path ordinary text, so
`repos/frontend/main.py` and `repos/public/index.html` stay as they are.
The route grammar of text applies to the owner (L1): a reserved route
word, a tool literal, a placeholder or a number is not learnt, the
repository still is (`repos/settings/<r>/pulls/5` is written
`repos/settings/project/pulls/5`). The reader runs over every decoded
form of the string, as the host reader does. **A bare `repos/<o>/<r>`**
(nothing after the repository) was measured both ways over the bank
(the cells of the 0.20 to 0.25 verifiers and the corpus): read as an
identity it costs 32 corruption cells (30 of `tmp/verify025/cells25.py`, 2 of `tmp/verify024/cells24.py`: `repos/frontend/main.py`, `repos/settings/defaults.yaml`, `repos/public/index.html` read as a bare `repos/<a>/<b>` once the file name is the repository) and 3 corpus rows (`v25_body_led_by_repos_and_a_reserved_word_is_ordinary`, `v25_body_led_by_repos_and_a_plain_directory_is_ordinary`, `v25_review_path_field_led_by_repos_and_a_plain_directory_is_ordinary`), and closes 0 leak (tree `tmp/fix026/tbare`, `tmp/fix026/bank_tbare`, `tmp/fix026/corpus_tbare.log`); not read, it costs nothing, so it is
ordinary text (row `v26_bare_text_api_path_is_ordinary`). GitLab's
`projects/<a>%2F<b>` keeps `_ENCODED_PROJECT_RE`. Named test
`test_a_text_github_api_path_names_a_repository_only_through_a_rest_route`;
rows `v26_body_led_by_a_github_api_path_with_a_rest_route_is_replaced`,
`v26_method_kwargs_github_api_path_with_a_rest_route_is_replaced`,
`v26_stdin_ghe_api_v3_path_with_a_rest_route_is_replaced`,
`v26_json_string_in_a_body_with_a_leading_slash_api_path_is_replaced`,
`v26_review_path_field_github_api_path_with_a_rest_route_is_replaced`,
`v26_text_api_path_with_a_reserved_owner_learns_the_repository_only`,
`v26_text_api_path_with_a_file_after_the_repository_is_ordinary`,
`v26_bare_text_api_path_is_ordinary`.
(c) **Every exchange's argv is read** (the 0.25 verifier's mutant B09,
`_argv_strings` walking the first member of a list, survived: no test had
two exchanges). Named test `test_every_exchange_argv_is_read_by_construction`;
row `v26_second_exchange_argv_api_path_is_read_by_construction` (the
corpus harness's `Sample.second_api_path`: the method issues a second
call, whose argv names `repos/settings/<r>/pulls/5/comments`, a path only
the argv reading writes `repos/org/project/…`).
(d) **Facts corrected.** 0.25 (a) said dropping the 0.23 text reading
closed the `repos/frontend/main.py` variant "at no cost". The claim is
withdrawn: no earlier cell exercised the leak side, and the cost was the
34 leaks of (b). 0.25 (a) also said every other string is read by
`_ENCODED_PROJECT_RE` and `closure.hosts` alone; at 0.26 the text reading
also holds the GitHub API path of (b).
(e) **Counts.** Ten `v26_` rows join the corpus (8 planted, 0 refused, 2
ordinary): 871 rows = 602 planted + 269 ordinary = 797 written + 74
refused, 18 residuals; the sweep holds 6 475 cells, unchanged.
What it could break, measured: a text path `repos/<a>/<b>/<word>` whose
`<word>` is one of the 74 REST words but which names a directory
(`repos/frontend/src/tags`) learns `<a>` as a namespace and `<b>` as a
repository; no cell or row of the bank holds one, and the corpus's
ordinary texts are unchanged.

Amendment (0.27, 2026-11-24, one implementer, after the 0.26 verifier's
NO-GO; the owner's decision of 2026-11-24, **structure versus prose**,
fixed and not re-litigated; only the red items, every green rule
untouched, `tmp/fable021/DEFERRED.md` still deferred).
(a) **The provenance table splits structure from prose.** A GitHub-form
API path `[/][api/v<N>/]repos/<o>/<r>/<REST word>…` names an identity only
in the **structured sources of a recorded call**: its argv, its stdin
(raw or JSON), its stderr, the field values of its JSON stdout, and the
method's positional and keyword arguments, nested or JSON-encoded. It is
never read in **prose**: a comment's or a change request's body (GitHub
`body`, GitLab's merge request `description` and note `body`), a code
block inside one, a review comment's file path (`path`, GitLab's
`position.new_path`/`old_path`), and a stdout that is not JSON (a diff,
a raw answer). `PROVENANCE` gains its fifth row, `structured` →
`api_path`, after `text`: `_structured_strings` walks the raw recording
in its order (an exchange is an object with an `args` list and a
`stdout`, as `_argv_strings` reads it; a JSON string is walked inside,
keys included) and skips the value of every `_PROSE_KEYS` key
(`body`, `description`, `path`, `new_path`, `old_path`: the keys the
forges read as a body or a review path) and every non-JSON stdout;
the `api_path` reader runs `_api_path_projects` (0.26's reader, renamed)
over every decoded form of each string. The `text` reading goes back to
its 0.25 content for API paths, which is none: hosts and the paths
after them, and GitLab's `projects/<a>%2F<b>` (`_ENCODED_PROJECT_RE`),
unchanged. Inside a structured string a path whose last segment is a
file name (`_FILE_NAME_RE`, `\.[A-Za-z][A-Za-z0-9]{0,5}$`) followed by
more words of a sentence (`_SENTENCE_AFTER_RE`, read where the path's
token ends at a `_PATH_END` character: commas or question marks, a
space, a word character; a dot, colon, semicolon or bang after the file
name is inside the token) is a file cited in prose and names no one
(`repos/<a>/<b>/releases/v1.md documents …` in a stdin); followed by a
query, a fragment, a closing delimiter or nothing it is a call and is
read (`GET repos/<o>/<r>/contents/README.md#L3`). This closes the 74
corruption cells of `tmp/verify026/cells26.py` (directory and file
paths under `repos/` in a body, a review path, a code block, a stdin
sentence, a GitLab body) and the one of `cells26b.py`. The AST test now
also pins every `_add("segment", …)` call inside `_segments` or a reader
the table dispatches, `_structured_strings` once as a source and
`_api_path_projects` once, inside the `api_path` reader. Named tests
`test_the_provenance_table_is_the_one_rule_of_what_names_an_identity`,
`test_a_github_api_path_names_a_repository_only_through_a_rest_route`
(0.26's `test_a_text_github_api_path_…`, its prose cases moved to a
stderr), `test_an_api_path_is_read_in_the_structured_sources_of_a_call_only`;
rows `v27_stderr_api_path_with_a_reserved_owner_learns_the_repository_only`,
`v27_stderr_api_path_in_an_error_line_is_replaced`,
`v27_method_positional_arg_api_path_is_replaced`,
`v27_stdin_api_path_to_a_file_with_a_fragment_is_replaced`, and the
ordinary rows `v27_body_directory_path_under_repos_is_ordinary`,
`v27_review_path_directory_under_repos_is_ordinary`,
`v27_code_block_directory_path_under_repos_is_ordinary`,
`v27_monorepo_layout_under_repos_is_ordinary`,
`v27_stdin_file_cited_in_a_sentence_is_ordinary`.
(b) **NG4-16, an API path cited in prose**, is a named residual: written
as it stands, covered later by SP-NO-PERSONAL-DATA-GATE and its optional
NER oracle. The leaks that come back by design are exactly the 0.25 leak
cells that sit in prose, each NG4-16 and nothing else, measured by
`tmp/fix027/attrib.py` over the bank: `cells25.py` 12 (`L_body_led_*`,
`L_json_in_body_*`, `L_path_field_*`, `L_raw_stdout_led_*`), `cells26.py`
48 (`L_path_*`, `L_nested_*` — a JSON string in a body —,
`L_body_mid_*`, `L_raw_stdout_*`, `L_gl_body_*`), `cells26b.py` 7 (the
six `L_body_*` and `O_upper_reserved_word`, a body). The four 0.26 rows
that pinned the prose reading become NG4-16 residual rows:
`v27_body_led_by_a_github_api_path_is_prose_NG4_16`,
`v27_json_string_in_a_body_is_prose_NG4_16`,
`v27_review_path_field_api_path_is_prose_NG4_16` (the path asserted as
written, `Written.fields` reading `path`),
`v27_reserved_owner_api_path_in_a_body_is_prose_NG4_16`; with
`v27_code_block_api_path_is_prose_NG4_16`,
`v27_gitlab_body_api_path_is_prose_NG4_16` and
`v27_raw_stdout_led_by_an_api_path_is_prose_NG4_16` (a residual row now
compares its text with the raw text at its position, `_planted_text`).
Named test `test_an_api_path_cited_in_prose_is_the_named_residual_ng4_16`.
(c) **Mutants.** Every non-equivalent survivor of the 0.26 verifier's
`tmp/verify026/mut26v.py` is killed by a named test:
`test_the_api_path_reader_reads_a_whole_path_between_its_bounds` (X01–X05
the lookbehind `% . _ ~ -`, X06 a percent-encoded owner learnt in its
decoded form only, X07 one `api/v<N>/` prefix, X08 a version of two
digits, X09 a one-character owner keeps its repository, X10 no route
across a space, X12 `pulls:`/`pulls;`, X13 casefolding, X14 a `.git`
owner learnt as written, X16 a reserved owner in any case),
`test_inside_a_source_the_first_reading_in_recording_order_keeps_the_class`
(X21, X22: two URL fields of one answer, of two exchanges, two stderr,
method arguments), `test_a_url_field_on_a_single_label_host_learns_the_host`
(X26). X16 was a missing test, not a wrong rule: the reserved check was
already casefolded at 0.26, and the mutant that dropped the casefold
learnt `Settings`; the test now holds it.
(d) **Facts corrected.** 0.26 (e) said of a directory path
`repos/<a>/<b>/<REST word>` that "no cell or row of the bank holds one";
true of the bank, and the understatement of the round: such a path is
the common case, not an edge, and the 0.26 verifier's cells corrupted
74 of 94 ordinary directory cells with it, prose included. 0.26 (b) read
that path in every string; 0.27 (a) reads it in the structured sources
only.
(e) **Counts.** Sixteen `v27_` rows, four of them the 0.26 rows renamed
(11 planted, 7 of them residual, 0 refused; 5 ordinary): 883 rows =
609 planted + 274 ordinary = 809 written + 74 refused, 25 residuals.
What it could break, measured: an API path in a structured source that
is a directory, with no file name (`repos/frontend/src/tags` in a
stderr), is still read; a file a sentence cites inside a structured
string names no one, so `repos/<o>/<r>/contents/README.md was not found`
in a stderr is NG4-16; neither is in the bank or the corpus.

Amendment (0.28, 2026-11-24, one implementer, after the 0.27 verifier's
NO-GO; the owner's two decisions of 2026-11-24 fixed and not
re-litigated: **structure versus prose**, and **no sentence rule: prose
is declared by the call, never guessed from its wording**; only the red
items, every green rule untouched, `tmp/fable021/DEFERRED.md` still
deferred).
(a) **The sentence rule is withdrawn.** `_FILE_NAME_RE`,
`_SENTENCE_AFTER_RE` and the "cited file" exception of 0.27 (a) are
gone: inside a structured string an API path
`repos/<o>/<r>/<REST word>…` is read whatever follows it, a sentence, a
comma, a colon, a newline, an HTTP status. This closes the 60 leaks of
`tmp/verify027/cells27.py` (error and log lines in a stderr, a stdin,
kwargs and a JSON field: `…/README.md: HTTP 404`, `GET …/README.md
HTTP/1.1`, `…, retrying`, `…\nexit status 1`) and the three of
`demo_w.py`; it brings back, by the owner's decision, the rewrite of a
sentence written in an undeclared structured channel: 11 `O_stdin_*`
cells of `cells26.py` and 7 `O_stderr_*` cells of `cells27.py`
(`repos/payments/ledger/releases/v1.md documents the ledger …` in a
stdin or a stderr learns `payments` and `ledger`). Row
`v27_stdin_file_cited_in_a_sentence_is_ordinary` becomes the planted row
`v28_undeclared_stdin_sentence_is_a_structured_source`; with
`v28_stderr_file_then_words_is_read`.
(b) **Prose is the channel the call declares** (`_declared_prose`,
`_prose_parameters`, `_PROSE_KEYS`). It is part of the provenance rule:
neither the `argv` reading (`_argv_strings`, hard) nor the `structured`
reading (`_structured_strings`, soft) reads a declared value.

| Channel | Declared by | Source in the forge code |
|---|---|---|
| argv field value | `-f`/`-F`/`--field`/`--raw-field` with a key of `_PROSE_KEYS` (`body=`, `path=`, `title=`…), separate, `-f<field>`, `-f=<field>` or `--field=<field>` | `github.py` `_pr_review_comment`, reply (`-f body=`, `-f path=`) |
| argv flag argument | the argument after `--body`, `--title`, `--message`, `--description`, or joined (`--body=<text>`) | `gh pr create --title --body`, `gh pr comment|review --body`, `glab mr create --title --description` |
| stdin | `--body-file -` (or `--body-file=-`), a prose field whose value is `@-` (`-F body=@-`) | `gh pr merge --squash --body-file -`; `--input -` sends JSON, whose prose keys are `_PROSE_KEYS` |
| positional argument | a parameter of `protocol.Forge.<method>` whose name is a prose key | `post_comment(number, body)`, `reply(number, comment_id, body)`, `upsert_marked_comment(number, marker, body)` |
| JSON key, any depth | `_PROSE_KEYS`, matched as written: `body`, `description`, `title`, `message`, `messageHeadline`, `messageBody`, `diff`, `path`, `new_path`, `old_path` | audited over every answer, stdin and argument of `tests/fixtures/forge/**` (`tmp/fix028/keys_audit.py`): the other keys are ids, states, dates, refs, URLs and labels (`name`, `workflow`) |

`known_values` takes `method` (the live recorder passes it, the offline
transform reads the fixture's `method`); an argument the signature does
not name, and every argument of an unknown method, stays structured, so
the 0.27 row `v27_method_positional_arg_api_path_is_replaced` is
unchanged. `glab` records no raw-stdin body: its write payloads go
through `--input -` as JSON. This closes the 0.27 verifier's red 1 (a
posted body and review path in the argv, the positional body; the four
older `--body` corruptions, which the hard `argv` reading made since
0.24) and red 2 (`title`, `message`, `messageHeadline`, `messageBody`):
`tmp/verify027/repro_post.py` writes the body and the path as they
stand; `tmp/fix028/cells28.py`, 276 cells over the fifteen prose values
and three review paths of the real write-method fixtures (each with a
planted stderr control) through the offline transform: 0 LEAK, 0
CORRUPTION at the head (89 CORRUPTION at `3cd7f8e`, 120 at `0d604fe`).
(c) **The corpus harness sends a row's stdin to the call only**, as a
real method does; it no longer echoes it into the method's kwargs (which
made every stdin row a kwargs row too, so a declared stdin could not be
pinned). No row changed by it; the one deferred `stdin differs` parity
case of `sweep_v2.py`/`sweep.py`/`probe1.py`/`probe_b.py` (the harness's
two copies of the stdin rewritten apart) is now the named refusal
`unknown mailbox at exchanges[0].stdin`.
(d) **Fact corrected.** 0.27 (a) said a comment's body and a review
comment's path are "never read". False at 0.27: carried in a write
call's argv (`-f body=`, `--body`, `-f path=`) or as the method's
positional body they were structured strings, and the hard `argv`
reading read an argv element led by `repos/` since 0.24. 0.28 (b) makes
the statement true for every channel the call declares.
(e) **Remaining cells, named** (L6). Head LEAK/CORRUPTION per set:
`cells25` 12/0, `cells26` 48/11, `cells26b` 7/0, `cells27` 0/21,
`cells28` 0/0, `demo_w` 0/1; `cells021`–`cells24` identical to every
earlier tree. Every LEAK is NG4-16 prose (`tmp/fix027/attrib.py`).
CORRUPTION: 11 `cells26` `O_stdin_*` and 7 `cells27` `O_stderr_*` are
prose in an undeclared structured channel (a); 7 `cells27`
`O_posarg_*` and 7 `O_argv_body_*` put the body as an extra positional
argument of `conversation_comments(number)`, which no recording can
hold (the live recorder refuses every write method and the signature
names no such argument; the same harness channel carries the planted
`L_args_*` leaks of `cells25`/`cells26`, which stay closed), and their
real counterpart, `post_comment`, is 0/0 in `cells28`; 1 `demo_w` W15
is `{"Body": …}`, a key no forge writes (keys are matched as written).
(f) **Counts.** Twenty-three `v28_` rows, one of them the renamed 0.27 row
(5 planted, 0 residual, 0 refused; 18 ordinary): 905 rows = 614
planted + 291 ordinary = 831 written + 74 refused, 25 residuals.
What it could break, measured: a sentence written in a stderr, an
undeclared stdin or a non-prose field (the 18 cells of (e)) is rewritten
where it names a REST route under `repos/`; a prose value a forge sends
through a flag or key outside the table above would be read.

## Intent

Child 2 of SP-REPO-CARRIES-NO-PERSONAL-DATA. SP-SCRUB-PERSONAL-DATA-AT-HEAD
rewrites the recorded forge fixtures by hand to class words; this spec
makes the recorder write that shape itself, so the next
`scripts/record_forge_fixture.py` run against a real pull request
cannot bring a handle, a real name, an avatar URL or a user id back
into `tests/fixtures/forge/`.

## Threat model

(0.12, owner decision 2026-11-18.) The recorder protects against
personal data that leaks **by accident** from real pull requests —
logins, real names, user and repository ids, private hosts, namespaces,
repository names, home paths, public IPv4 addresses, personal mailboxes,
branch names, Co-authored-by noreply trailers, URLs with userinfo or
double slashes, and embedded GitHub user node ids. It does **not**
protect against an adversary who crafts text on purpose to evade the
recorder or its closure oracle. Forms that require deliberate
obfuscation (Unicode confusables, invisible characters, Markdown
splitting or escaping, non-node-id base64 payloads, JSON-in-a-string
escapes) are out of scope and accepted under NG4: what 0.11 already
handles for them is kept as best-effort defence-in-depth, without any
claim that they are caught in general.

## Context

### Measured on `origin/develop` `0be2a56` (2026-11-18)

| # | Fact | Where |
|---|---|---|
| F1 | The recorder keeps an allow-list of keys and drops everything else, recursively; `login`, `name`, `username`, `id`, `url` and `web_url` are kept | `src/repoach/forge/recording.py:32` (`RECORDED_FIELDS`), `:74` (`"login"`), `:82` (`"name"`), `:122` (`"url"`), `:124` (`"username"`), `:125` (`"web_url"`), `:142-145` (`redact_payload`) |
| F1b | The forges read `name` as a check-run or job name, `url` as a pull request's URL and `web_url` as a merge request's, a pipeline's and a job's URL; none reads `avatar_url`, `html_url`, `full_name` or `email` | `src/repoach/forge/github.py:281,1776,1809` (`entry.get("name")`, `r.get("name")`), `:501` (`payload.get("url")`); `src/repoach/forge/gitlab.py:223` (`mr.get("web_url")`), `:982` (`pipeline.get("web_url")`), `:1016` (`job.get("name")`), `:1021` (`job.get("web_url")`); `git grep -n -E '\.get\("(avatar_url\|html_url\|full_name\|email)"' src/repoach/forge` → 0 |
| F1c | The keys the readers consume from a user object (`user`, `author`, `approved_by[].user`): `login` (`github.py:239,489`), `username` (`gitlab.py:171,725`) and the numeric `id` (`gitlab.py:728`, `Review(id=_int(user.get("id")) or 0)`); no reader consumes a user's `name`. The GitLab approvals fixture expects the user ids `11` and `12`; a dropped user `id` would replay as `0` | `git grep -n -E '(user\|author)\.get\("' src/repoach/forge/{github,gitlab}.py` → those five lines; `tests/fixtures/forge/gitlab/reviews_two_approvals.json` (`"id": 11`, `"id": 12`); the GitHub GraphQL `author.id` is the user's node id (`MDQ6…`), read by nothing, present in the three fixtures of F3 |
| F2 | Secret-shaped keys are dropped by name: token, secret, password, private_key, api_key, `node_id`, email | `src/repoach/forge/recording.py:138` (`SECRET_KEY_RE`), `:191-194` (`_secret_keys`) |
| F3 | Three fixtures carry the maintainer's handle as `login`, the real name as `name` and the GitHub user node id as the `id` of the recorded `author` object | `tests/fixtures/forge/gate/pr202_capture.json:17`; `tests/fixtures/forge/github/open_pull_requests.json:24`; `tests/fixtures/forge/github/pull_request.json:19` (parent F9) |
| F4 | Twelve fixtures carry the handle, in `login`/`username` values and in recorded argv (`collaborators/<handle>/permission`, `members/all?query=<handle>`) | `tests/fixtures/forge/github/*.json` (7), `tests/fixtures/forge/gitlab/is_maintainer*.json` (4), `tests/fixtures/forge/gate/pr202_capture.json` (parent F8) |
| F5 | Replay is argv-for-argv: the first argv that differs from the recording fails the conformance suite | `src/repoach/forge/recording.py:1-13` (module docstring), `:285-300` (`ReplaySpawn.__call__`) |
| F6 | The recording script is a frontier file (no owner) and imports the recording module; the recording module is owned by SP-UNIVERSAL-FORGE | `scripts/record_forge_fixture.py:18,29-37`; `Registry.owner_of("src/repoach/forge/recording.py")` → `SP-UNIVERSAL-FORGE`, `owner_of("scripts/record_forge_fixture.py")` → `None` |
| F7 | The redaction rule the fixtures follow is SP-SCRUB-PERSONAL-DATA-AT-HEAD D1 (`format:redaction-rule`): login → class word, name and user id dropped, host → placeholder | that spec's D1 table |

## Decisions (2026-11-18, rewritten at 0.17 on 2026-11-22)

The guarantee (D3, D7, D8) is stated first; the placeholders (D1, D1b,
D1c, D2) are the quality layer under it. Every rule below names the test
that turns red when it is broken; the acceptance corpus
(`tests/unit/test_forge_recording_acceptance_corpus.py`, AC1) pins the
whole behaviour row by row.

- D7 (0.17). **Known private values are replaced everywhere, as exact
  tokens.** `known_values(raw, *, roles, remote, denylist) -> Known` in
  `src/repoach/forge/recording.py` builds, before any rewrite, the set `K`
  of `(kind, ordinal, value)` the fixture must not hold, from these
  sources and no other guess:
  1. **structured fields of the recorded exchanges** (the decoded JSON
     answers, the argv, the method's arguments, the checkout's `origin`
     remote): every value under `login`/`username`/`author`/`resolved_by`/
     `user` (kind `login`, with `raw_logins` of the raw stdout so a login
     under a dropped key counts), every `id` of a user object in every
     form `user_id_key` names (the number, its digit string, the legacy
     node id padded and unpadded, the current `U_` node id — kind `id`),
     every `id` of a repository object (kind `id`), a user object's `name`, or
     the `name` beside a mailbox of a git author or committer object that holds
     no `login`, and a GitLab commit object's `author_name` and
     `committer_name` beside `author_email` and `committer_email` (0.18, 0.19; kind `name`: the whole value, which is judged but not replaced, and
     each of its words of at least `NEEDLE_MIN_LENGTH` letters or digits,
     which are replaced: row `name_multiword_name_in_title`), a user object's `email`,
     `public_email` or `commit_email`, and a commit object's `author_email` and
     `committer_email` (kind `mailbox`: the whole mailbox
     and its local part when at least `NEEDLE_MIN_LENGTH`; its host is a
     `host` value), the host of every URL under `URL_KEYS` and of the
     remote (kind `host`; `FORGE_HOSTS` always), and the namespace and
     repository of the remote's path, of every `URL_KEYS` value on a host
     that is not a public forge's, and of every argv API path
     (`repos/<o>/<r>`, `projects/<o>%2F<r>`, the CLI templates aside;
     an argv string of a recorded call and no other string, 0.25; each
     source and its reading are one row of `PROVENANCE`, 0.26; a GitHub
     API path `repos/<o>/<r>/<REST word>` in the structured sources of a
     call, never in prose, 0.27, NG4-16; prose is the channel the call
     declares, never a string's wording: a prose key, a prose argv field
     or flag, a declared stdin, a positional argument the method's
     signature names, 0.28;
     kind `segment`, through `path_identity`);
  2. **the deny-list** (kind `deny-list`; a numeric entry also in its two
     node-id forms);
  3. **the text reader of the oracle** (D8, `closure.hosts`, run over every
     decoded form of the string, 0.18): every
     host-shaped token found in any string of the raw exchanges that is
     neither a placeholder-class host nor on the public allowlist is a
     `host` value, and the namespace and repository of the path that
     follows it are `segment` values when the host is not a public
     forge's (through `path_identity`: every segment of a URL behind a
     scheme on the recorded instance, the first two `/<a>/<b>` after any
     other lead, so `//cdn.example.io/<ns>/<repo>/app.js` keeps
     `app.js`; rows `r10_a_proto_rel_*`, `reg_url_ftps_*`). **0.20**: the
     path is read only after a **led** host or a followed label host,
     never after a followed address (a followed address is the oracle's
     to judge, NG4-2); a text path yields values only when it holds both
     a namespace and a repository, or names an owner through an API root
     (`_api_route`: `users/`, `orgs/`, `repos/`, `apps/`, `groups/`,
     `api/vN/`), or stands before a GitLab `/-/` separator (0.21, B1:
     `<group>/-/issues`, `<user>/-/snippets`, `groups/<g>/<sub>/-/epics`),
     or sits in the identity slot of a route grammar (0.22: the Web IDE's
     `-/ide/project/<ns>/<repo>/…`, Bitbucket Server's
     `users/<u>/repos/<r>` and `projects/<KEY>/repos/<r>`, GitHub's
     `orgs/<o>/teams/<t>`) — a reserved route word (`uploads`, `help`,
     `explore`, `-`, `marketplace`, `.well-known`, `_RESERVED_ROUTE_WORDS`)
     is never an identity and a package tarball `<pkg>/-/<pkg>-<v>.tgz`
     names no one (0.22), and what follows `packages/npm/` of GitLab's
     own npm registry (`api/vN/…/packages/npm/`, `_npm_registry`) names
     no one, its `@<scope>` included: a scope read from a URL is never
     learnt on any registry (0.24, NG4-15; 0.23 read it as a root group
     on a false premise); a lone segment (`/docs`, `/health`, `/explore`)
     is a page and is never learnt (NG4-11); nothing is read after a placeholder-class or public
     host (NG4-12). Structured sources (the remote, `URL_KEYS`) keep
     reading a lone segment; the remote and an argv API path
     (`_api_project`: `repos/<o>/<r>/…`, `projects/<ns>%2F<r>/…`) are
     not URL paths and read as `<ns>/…/<repo>` by construction
     (`path_identity(remote=True)`, 0.23, 0.24), so a namespace that is a
     reserved word (`assets`, a real GitHub account) is a value, and a
     namespace of two segments or more is a value as each of its leading
     paths too (`<a>/<b>` → `org`, 0.24), while a `URL_KEYS` value keeps
     the route grammar. A `projects/<a>%2F<b>`
     token in any string yields `a` and `b` as segments; a GitHub API
     path `[/][api/v<N>/]repos/<o>/<r>/<word>` in any string, not inside a
     longer path or a URL, yields `o` and `r` when `<word>` is a REST
     repository sub-resource (`_GITHUB_REPO_API_ROUTES`, 0.26; an owner
     that is a reserved word, a literal or a number is not learnt); a project id by
     D9 is an `id` value.
  A value shorter than `NEEDLE_MIN_LENGTH` (4) or equal to a class word
  or a placeholder is a configuration error when it comes from source 1
  (`NeedleError`, `needle <kind> <n>: too short` / `: a class word or
  placeholder`, exit 2, as at 0.10) and is silently not a value when it
  comes from source 3 (a third party's short segment must not make a
  recording unrecordable; the host itself is still a value). A
  `NON_PERSON_LOGINS` login, with or without `[bot]`, is no value; a
  `[bot]` login's stem follows D2: its `@<stem>` mention is a login value
  always, and its bare stem only when the recording mentions it (rows
  `reg_renovate_with_actions_checkout_title_and_docs_github` and
  `r10_o32_renovate_mend_io_and_docs_renovatebot`). The deny-list entries
  stand in `Known` as kind `deny-list`, judged by `residual` after the six
  kinds and never replaced.
  **Replacement** (`replace_known(text, known, mapping) -> str`): every
  value is replaced in every string of the fixture — every exchange's
  stdout (JSON values and raw text alike), stdin, stderr and argv, the
  method's `args` and `kwargs`, `source` after its head, every
  key outside `CODE_KEYS` being judged but never rewritten (a forge reads
  it), and `case` being judged but never rewritten (it is the author's own
  word: rows `reg_case_field_*` refuse a known value in it) — by its class token: a login by its role word (D2), a host by
  `forge.example.com`, a namespace by `org`, a repository by `project`, a
  user id by its synthetic number and a node id by `U_<n>` (D1c), a
  repository id by its synthetic `2001..2999` number, a name word and a
  mailbox local part by the class word of their user, a deny-list entry
  by nothing (the list refuses, it never redacts; D3). Longest value first.
  The boundary is one function, `token_pattern(word)`: letters and digits
  are inside a token, everything else delimits, `re.I`, casefolded on both
  sides; a `%2F` or `%2f` between two tokens is a delimiter as well
  (`projects/<ns>%2F<repo>` is the one URL-encoded shape the forges' own
  argv carries, so `zqgroup%2Fzqmainrepo` reads `org%2Fproject`); a node
  id is matched with its `=` padding when it has one. Replacement runs
  after the convenience redaction (D1b) and before the oracle (D3), so a
  shape the convenience layer did not recognise is still cleared, and a
  value it already turned into a placeholder is not touched twice. Pinned
  by the acceptance rows: `g_<class>_<carrier>` (eight host classes, ten
  carriers), `pos_*` (every position), `uid_*`, `pid_*`, `name_*`,
  `email_*`, `code_three_label_private_host_*`, the round-10
  `r10_a_proto_rel_*` and `r10_a2_*` shapes, and by
  `tests/unit/test_forge_recording_redaction.py::test_known_values_are_replaced_as_tokens_in_every_position_by_construction`
  (every source of `K`, every kind, every position, `%2F`, padding,
  longest-first; mutating any one of them turns it red).
- D8 (0.17). **Host-shaped, mailbox-shaped and public, by construction.**
  `src/repoach/forge/closure.py` defines, with explicit sets and one short
  reader each, never a regex shared with `recording.py`:
  - a **label run**: `L1.L2…Ln`, `n >= 2`, each label `[A-Za-z0-9-]{1,63}`
    not starting or ending with `-`, an optional trailing `.`, delimited by
    any character outside `[A-Za-z0-9.-]`; `Ln` alphabetic, at least two
    letters;
  - **H1, led**: the run is immediately preceded by `<scheme>://`
    (`URL_SCHEMES` or any `[A-Za-z][A-Za-z0-9+.-]*://`) or by
    `<local>@` (a non-empty run of `LOCAL_CHARS` = letters, digits,
    `._%+-:${}` right before `@`); every such run is a host, whatever `n`;
  - **H2, led by `//` or followed** (0.18): `n >= 3`, `Ln` in `HOST_TLDS`,
    `L1` not in `REVERSE_DNS_HEADS` (`com`, `org`, `net`, `io`, `dev`, `me`,
    `co`, `edu`, `gov`: Java/Android/Maven reverse-DNS names such as
    `com.example.app` and `org.apache.commons.io`), and the run either
    right after `//` or right before `/<segment>` (a letter, a digit or one
    of `_-.~%` after the slash) or `:<port digits>`. **A dotted name that is
    neither led (by a scheme, `<local>@` or `//`) nor followed is ordinary
    text, whatever its last label** (`response.data.id`, `self.logger.info`,
    `cfg.Server.Host`, `Host mirror.zqcorp.net is down`): the rule is
    stated here and in `closure.hosts`, nowhere else. A host known from a
    structured field is replaced everywhere, led or not (D7). A
    protocol-relative `//` lead is **not** H1: `//<run>` needs three labels,
    so `x := 1 //cfg.dev/env/prod` is a comment and
    `//code.kvncloud.io/ns/repo` is a host, in every quoting, spacing and
    assignment context alike. `NON_HOST_TOKENS` is deleted. A percent
    escape (`%2F`) before a run is a delimiter, never part of its first
    label;
  - `HOST_TLDS`: the explicit frozenset of every ISO 3166-1 alpha-2 code and
    of the reserved or retired country codes `uk`, `eu`, `su` and `ac`,
    the generic TLDs listed in the module (`com`, `net`, `org`, `edu`,
    `gov`, `mil`, `int`, `info`, `biz`, `name`, `pro`, `dev`, `app`, `io`,
    `cloud`, `tech`, `site`, `online`, `xyz`, `co`, `me`, `tv`, `ai`,
    `ly`, `to`, `fm`, `gg`, `gl`, `ws`, `wiki`, `page`, `live`, `run`,
    `team`, `works`, `zone`, `network`, `systems`, `services`,
    `solutions`, `software`, `engineering`, `digital`, `agency`, `studio`,
    `host`, `hosting`, `domains`, `email`, `codes`, `tools`, `build`,
    `one`, `world`, `global`, `group`, `company`, `ltd`, `inc`, `llc`,
    `gmbh`) and the private-use suffixes (`internal`, `local`, `lan`,
    `intranet`, `home`, `corp`, `localdomain`, `test`, `example`,
    `invalid`, `localhost`), **minus** `EXTENSION_TLDS` = `ac`, `am`,
    `cc`, `cr`, `cu`, `gd`, `in`, `md`, `mk`, `ml`, `mm`, `pl`, `pm`,
    `pro`, `ps`, `py`, `rs`, `sc`, `sh`, `so`, `st`, `sv`, `tf` (0.20: the
    class is the source, build and binary file extensions a compiler or
    a linter cites as `<file>:<line>`, the one follow shape a file name
    shares with a host: `config.h.in:5`, `settings.local.py:10`,
    `kernel/foo.bar.cu:12:3`, `core.alu.sv:33` are files). A self-hosted
    forge on one of those twenty-three codes is learnt when led (H1) or
    structured (D7), not un-led (NG4-3);
  - **IPv4** (0.19, owner decision: it follows the host rule; 0.20): a
    dotted quad with token boundaries is an address **only when it is led**
    (a scheme `://`, `//` or `<local>@`: rows `g_ipv4_*_led_proto_rel_*`)
    **or followed** (`:<port digits>` or `/<segment>`), whatever its
    octets, and not in a **version context** (`_version_context`, by
    class: a path segment — a single `/` right before it, not `//`, so
    `/ojdbc8/19.3.0.0/ojdbc8-19.3.0.0.pom` and `/x/1.2.3.4/files` are
    versions and `1.2.3.4/24` is still an address; a comparator `==`,
    `>=`, `<=`, `~=`, `!=` before it with any spaces or tabs; the label
    `version` before it with any run of ` \t:=` separators, `Version:
    1.8.0.252`, `version = 1.8.0.252`; a `-` or `+` right after it, a
    pre-release or build tag). A led address is a host: replaced by the
    recorder (D7 source 3) with its namespace and repository read, and
    `unknown host` for the oracle when it is neither placeholder-class
    nor public. A followed address is judged and its path is **never
    read**: public (outside `IPV4_RESERVED`) it is refused, reason
    `public address` (`104.16.0.1:8080`, `104.16.0.1/kvnteam`); private,
    loopback or reserved it is written as it stands (`curl
    10.0.0.12:8080/metrics`, `10.1.2.3:8080/<ns>/<repo>`: NG4-2). A
    placeholder-class address (loopback, `0.0.0.0`, `255.255.255.255`, RFC
    5737) is skipped before any read. A **bare quad in prose is ordinary
    text**, version or address alike (`JDK 1.8.0.252`, `pkg = 1.2.3.4`,
    `resolver 1.1.1.1`): NG4-9. The 0.18 zero-octet exception is deleted;
  - **IPv6** (0.18, 0.19, 0.20): a bracketed literal `[<hex groups>]`, with an optional
    `%<zone>` or `%25<zone>` and an optional dotted IPv4 tail (two groups,
    read through the IPv4 reader and built from its canonical form, so
    `[::ffff:01.2.3.4]` is an address and never an exception),
    that parses as an IPv6 address; led (by a scheme, `<local>@` or `//`) it is a host, un-led and
    global with more than two colons (`xs[2020::]` and `xs[::2]` are slices)
    it is a `public address`; the loopback, the unspecified `::` and the
    documentation prefixes `2001:db8::/32` and `3fff::/20` are
    placeholder-class, and nothing is read after a placeholder-class
    literal (NG4-12), as after a placeholder IPv4; the namespace and
    repository after a led private or global literal are read (D7 source
    3) and the convenience layer rewrites the whole URL (D1b: a bracketed
    host is a text URL, 0.20), port dropped like any rewritten URL;
  - **placeholder-class hosts** (`is_placeholder_host`): `forge.example.com`,
    `example.com`/`.org`/`.net` and their subdomains, any `.example`,
    `.test`, `.invalid`, `localhost`, `127.0.0.0/8`, the unspecified and
    limited-broadcast addresses `0.0.0.0` and `255.255.255.255` (RFC 1122:
    a bind or a log line, never a machine; 0.20, exact addresses only,
    `0.1.2.3` is reserved and still a host when led), the RFC 5737
    networks (0.17: `203.0.113.9` in a URL is kept, as the posture table
    has always said), and for IPv6 the loopback, `::` and the
    documentation prefixes;
  - **public allowlist** (`is_public_host`, option A, extensible by owner
    decision): the registrable suffixes of `PUBLIC_REFERENCE_HOSTS` — the
    18 of 0.15 plus `pythonhosted.org`, `cloudflare.com`, `jsdelivr.net`,
    `unpkg.com`, `renovatebot.com`, `mend.io` (round-10 Preference 1),
    `apache.org` (0.19: `repo.maven.apache.org`) and `nuget.org` (0.20:
    `api.nuget.org`), 26 suffixes — matched as the
    exact host or `.<suffix>`, trailing dot stripped, casefolded; a public
    forge host (`github.com`, `gitlab.com`, `githubusercontent.com` and
    subdomains) is **never** public here: a namespace on it may be the
    recorded person's, so it is a `host` value and becomes the
    placeholder;
  - **mailbox** (`mailboxes(text)`): `<local>@<host>` with `local` a
    run of `LOCAL_CHARS` holding a letter or a digit, its `mailto:` prefix
    dropped and its leading `._%+-$` ignored when judging the literals
    (`_git@forge.example.com`, `+git@…`, diff markers), and `host` a label
    run or an IPv4; it
    is a **literal** when the whole string is `git@<placeholder-class
    host>`, `noreply@forge.example.com`, `no-reply@forge.example.com`,
    `<class word>@users.noreply.forge.example.com` (the D1b noreply
    placeholders), or when `local` holds no `:` and `host` is a
    documentation domain of SP-NO-PERSONAL-DATA-GATE D1
    (`MAILBOX_DOC_DOMAIN_RE`, copied: exact `example.com|org|net`, suffix
    `.invalid|.test|.example|.localhost|.internal`, `localhost`) or a
    placeholder-class **address** (0.20: `root@127.0.0.1`, `deploy@0.0.0.0`,
    `admin@203.0.113.9` name no one, by the RFCs the placeholder class
    cites; `forge.example.com` is not such a host, NG4-5);
    everything else is `unknown mailbox`, including a credential
    (`local` with `:`, so `x-access-token:${GITHUB_TOKEN}@…` and the
    0.16 corruption `forge.example.com{GITHUB_TOKEN}@forge.example.com`
    are both refused when the convenience layer leaves them) and a
    personal mailbox on a subdomain of `example.com` (as the gate judges
    it).
  Every set is enumerated by a test that walks it: `HOST_TLDS` (every
  entry two to twelve letters, every `EXTENSION_TLDS` entry absent, every
  `REVERSE_DNS_HEADS` entry present as a TLD too), `IPV4_RESERVED`, the
  allowlist, the placeholder class, the 128 ASCII characters as token
  delimiters, `LOCAL_CHARS`
  (`tests/unit/test_forge_closure.py::test_host_rule_tld_lists_reverse_dns_heads_and_extension_codes_by_enumeration`,
  `::test_ipv4_rule_reserved_table_version_lead_and_led_addresses_by_enumeration`,
  `::test_mailbox_rule_literals_credentials_and_doc_domains_by_enumeration`,
  `::test_public_allowlist_and_placeholder_class_by_enumeration`). The
  ordinary corpus justifies the rule: every two-label code token of
  rounds 5–10 (`Xcode.app`, `config.dev`, `ssl.ca`, `stream.io`,
  `torch.net`, `os.path`, `self.config`, `np.array`, `file.py`,
  `README.md`, `svc.io`, `cfg.dev`, `render.io`, `app.io`, `fs.io`,
  `ret.io`, `spring.io`, `math.io`, `store.io`, `metrics.net`,
  `docs.rs`) is un-led and two-label, hence ordinary by construction, and
  every private host the verifiers planted (`git.zqcorp.io`,
  `mirror.zqcorp.net`, `code.kvncloud.io`, `git.kvnworks.net`,
  `gitlab.qwzcorp.internal`, `git.mvkother.io`) has three labels and a
  listed suffix, hence a host in every context (rows
  `code_*_comments_and_dotted_identifiers`, `code_macos_*`,
  `code_three_label_*`, `code_reverse_dns_*`, `r10_o14`–`r10_o20`,
  `r10_a_*`, `g_private_unknown_*`).
- D3 (rewritten 0.17). **The recorder refuses what the oracle finds, in a
  fixed order, naming a reason and a position, never a value.**
  `refuse_unclassified(fixture, denylist, known)` in
  `scripts/record_forge_fixture.py` judges, in this order, and raises
  `UnclassifiedValueError` on the first hit:
  1. every login-bearing value of every exchange is a class word or a
     `NON_PERSON_LOGINS` entry (`unclassified: <path>`, unchanged);
  2. for every string of `scanned_strings(fixture)` (exchanges first, then
     `args`, `kwargs`, `case`, `source` after its head, `result`; every
     key outside `CODE_KEYS` as a string named `{key <i>}`), in that
     order, and for each string in this order:
     - `generic class home_path at <path>` (`HOME_PATH_RE`, the one gate
       copy the recorder keeps);
     - `deny-list entry <n> at <path>` (`Denylist.hit`, literals kept,
       the entry's node forms included);
     - `residual <kind> <n> at <path>`: `closure.residual(text, known)`,
       the known values in kind order `login`, `host`, `segment`, `id`,
       `name`, `mailbox`, each as `token_pattern` over the text and over
       its decoded forms — four rounds of percent-decoding, HTML-entity
       decoding and string-escape decoding (`\uXXXX`, `\xHH` and `\/`,
       the JSON and C-family escapes a pasted payload carries: 0.20), every
       form casefolded, `%2F` a
       delimiter — and the account number of every user node id found
       embedded in a longer run (`node_id_numbers`: the legacy
       `MDQ6VXNl…` prefix anywhere in a base64 run, a `U_` run decoding
       to MessagePack `[0, n]`), the one 0.11 reader this oracle keeps
       because an embedded node id is a real accidental shape (a GraphQL
       error pasted in a comment); NFKC, `Cf` removal and the
       `CONFUSABLES` table are **not** carried over (NG4-4);
     - `unknown host at <path>`: `closure.hosts(text)` finds a host
       (D8) that is neither a placeholder-class host nor public;
     - `public address at <path>`: a public IPv4 that is followed and not led, or an un-led global IPv6 literal (D8, 0.19);
     - `unknown mailbox at <path>`: `closure.mailboxes(text)` finds one
       that is not a literal (D8).
  The ordinals count within their kind, as at 0.11. A `CONSTANT_FIELDS`
  value (`forge`, `method`) is judged by the deny-list only. `source`'s
  head is `RECORDED_SOURCE_HEAD_RE`. The deny-list stays opaque
  (`<denylist: n entries>`, `Needles` → `Known` reprs as a count, a
  failing check under `pytest -l` prints no entry).
  **The oracle is independent by construction and by test.**
  `src/repoach/forge/closure.py` imports `re`, `ipaddress`, `base64`,
  `html`, `urllib.parse` and `dataclasses` only — never
  `repoach.forge.recording`, never the scripts — and defines its own
  `token_pattern`, label-run reader, mailbox reader, IPv4 reader and
  decoders. `recording.py` imports `closure` (for D7 source 3 and for
  the placeholder and public predicates the convenience layer must agree
  with), the reverse never holds.
  `tests/unit/test_forge_closure.py::test_the_oracle_shares_no_code_object_with_the_redactor`
  imports both modules and asserts: no `re.Pattern`, function, class or
  frozenset object reachable from `closure.__dict__` is the same object
  (`is`) as one reachable from `recording.__dict__` or from either
  script's namespace; `closure` has no attribute whose `__module__` is
  `repoach.forge.recording`; `ast`-walking `closure.py` finds no `import`
  of `recording`, `record_forge_fixture` or `redact_forge_fixtures`; and
  no pattern source string of `closure` equals a pattern source string of
  `recording` (so a copy-paste is caught too). The oracle shares three
  readers' **outputs** with the pipeline, never their code: it receives
  `Known` (values and kinds) as data; which segment is a namespace
  (`path_identity`), which string encodes which account (`user_id_key`)
  and which keys hold a login (`login_values`) stay in `recording.py` and
  feed `K` (D7). A defect there is a missing value, not a blind position;
  a defect in the oracle's own readers is what AC3's mutation run must
  find.
  **The secret gate.** `record()` keeps `find_secrets` for the forge
  token shapes of `SECRET_VALUE_PATTERNS` (`glpat-`, `glrt-`, `ghp_`,
  `github_pat_` and the GitLab runner token) and no longer counts its
  mailbox pattern: `find_secret_tokens` in `recording.py` iterates
  `SECRET_TOKEN_PATTERNS`, the tuple without the mailbox entry (measured
  2026-11-22: 79 acceptance rows holding `git@forge.example.com:…`,
  `noreply@forge.example.com`, `author@users.noreply.forge.example.com`
  or `support@example.com` were refused by it as "secret-shaped"; a
  mailbox is the oracle's to judge, rule D8). A realistic token still
  refuses (`refusing to write a fixture carrying <n> secret-shaped
  value(s)`, row `r10_p21b_gh_api_realistic_ghp_token_refused`).
  **Accepted refusals** (the pipeline knowingly leaves the value to the
  oracle; each one a corpus row): a deny-list entry anywhere; a known
  value in an encoded or lookalike form the replacer does not decode
  (`%6a%64…`, `&#x…;`, `\u…`); a known value as a key outside
  `CODE_KEYS`; a mention-only login's bare word (D2); an embedded node id
  with an alphanumeric prefix or suffix; a personal mailbox, which the
  replacer turns into `<class word>@forge.example.com` and the oracle
  refuses as `unknown mailbox` (rows `email_dropped_key_mailbox_in_prose_refused`,
  `email_personal_on_private_known_host_refused`); a 3-character login or
  segment of the recorded project (`NeedleError`).
- D9 (0.17). **One project-id rule.** A token `projects/<n>` with `n` of
  at least five digits, in any string (`_PROJECT_ID_RE`, token-bounded:
  argv, `args`, `kwargs`, `stdin`, `stderr`, raw and JSON stdout,
  `result`, prose, a `glab api` line balanced or not, an
  `api/v<N>/projects/<n>` path, a web `projects/<n>/merge_requests/42`
  path) and the `id` of every repository object name a project; each is
  an `id` value of `K` (D7) and is remapped to `2001..2999`
  (`UserIdMap.for_projects`, D1c) in every position, the convenience
  layer never turning the number into `project` (`path_identity` treats
  `projects/<digits>` as an id segment). `synthetic_project_ids` declares
  the `id` of a repository object only: a synthetic number standing in
  text has four digits and is no project id. Four digits and fewer are never ids (`projects/2026/q1.md`,
  `/projects/2025/architecture.md`, `glab api /projects/42/issues`,
  `gitlab.example.com/api/v4/projects/1234/…` stay byte-identical; a
  self-hosted project numbered below 10 000 is the named residual,
  NG4-6). There is no kept-host exception and no `glab api` grammar.
  Pinned by rows `api_*`, `r10_c_glab_api_*`, `r10_i1_d4_*`, `r10_p15`–
  `r10_p20`, `r10_o10`, `r10_o13`, `r10_o36`, `r10_o37`, `reg_url_glab_api_*`,
  `reg_url_api_v*` and by
  `tests/unit/test_forge_recording_redaction.py::test_project_ids_are_five_digit_projects_tokens_in_every_position_by_construction`
  (four digits, five digits, every position, the year paths of 2020–2030).
- D1. **Personal keys are dropped by name, `name` by context, `id` kept
  as a key** — unchanged from 0.16 (`PERSONAL_KEY_RE`, `redact_payload`,
  `RECORDED_FIELDS` with `commits`/`messageHeadline`/`messageBody`, a
  user object's `name` and URL keys dropped, a check run's `name` kept,
  F1b/F1c). 0.17 adds only that a dropped `name`, `email`, `public_email`
  or `commit_email` value is read into `K` before it is dropped (D7).
- D1b (demoted 0.17). **Convenience placeholders, shape kept.** What
  stays, unchanged in behaviour and pinned by the corpus rows that
  already passed at 0.16: `redact_url` on `URL_KEYS` values and on text
  URLs of `URL_SCHEMES` (`http`, `https`, `ssh`, `git`, `ftp`, `ftps`):
  host → `forge.example.com`, namespace → `org`, repository → `project`
  (`.git` kept), the GitHub route allowlist (`_GITHUB_ROUTES`) and the
  GitLab `/-/` rule for what follows (`/pull/12` kept, `/wiki/Home`
  dropped; 0.21: everything before `/-/` is an identity — one segment the
  namespace, two or more the namespace and the repository, after an owner
  root such as `groups/` or `users/` every segment up to it the
  namespace — written `org/-/<route>`; 0.22: the grammars are one table —
  a reserved route word is never an identity and keeps its path as it
  stands, the Web IDE's `-/ide/project/<ns>/<repo>/edit/…` and the
  owner-root slots `users/<u>/repos/<r>`, `projects/<KEY>/repos/<r>`,
  `orgs/<o>/teams/<t>` are read, an npm tarball `<pkg>/-/<pkg>-<v>.tgz`
  is kept whole; 0.23: the `@<scope>` of GitLab's npm registry is
  written `@org` on the instance, project and group endpoints (0.24:
  withdrawn, the scope is written as it stands unless it is a known
  value), the roots
  are matched in any case, and a remote — not a URL path — reads
  `<ns>/…/<repo>` whatever its first word), query dropped, fragment dropped unless a comment anchor
  (`FRAGMENT_ANCHOR_RE`); the SCP normaliser (`<user>@<host>:<path>` read
  as `ssh://`, written `git@forge.example.com:org/project`, a
  line-leading `+`/`-`/space diff marker allowed); userinfo never kept,
  the whole URL replaced when it has one — and, 0.17, the userinfo class
  extended by `$`, `{` and `}` so `https://x-access-token:${GITHUB_TOKEN}@github.com/o/r.git`
  becomes `https://forge.example.com/org/project.git` instead of the
  corrupted `https://forge.example.com{GITHUB_TOKEN}@forge.example.com/…`
  (R10-D2; rows `g_*_https_var_userinfo`, `r10_rg_ci_*`, `r10_ci_insteadof_*`);
  the kept hosts (placeholder-class and public, D8, scheme and host
  lowercased, userinfo stripped, path and query still subject to D7);
  the trailing-punctuation and Markdown-delimiter rules of 0.13–0.16
  (`_URL_TRIM_TAIL`, `_split_url_tail`, `_strip_balanced_tail`); the
  noreply rules (`<id>+<login>@users.noreply.<host>` →
  `author@users.noreply.forge.example.com` / `bot@…`, system noreply and
  `actions@github.com` → `noreply@forge.example.com`); a repository
  object's `name`/`path` → `project`, `description` dropped. What is
  gone: the protocol-relative grammar — a `//host/path` is no longer
  rewritten by shape in any context; its host, namespace and repository
  are replaced as known values (D7) and its path is kept
  (`<script src="//forge.example.com/org/project/app.js">`, rows
  `r10_a_proto_rel_*`, `reg_r10_protocol_relative_*`); the `glab api`
  tokeniser (D9); `unredacted_host` (D3 judges hosts); the mailbox and
  IPv4 generic classes (D8). Nothing in this decision is a safety claim:
  a row of the corpus that names a convenience output pins that output,
  and D7 clears what the convenience layer misses.
- D1c. **User and repository ids are synthetic and declared** —
  unchanged from 0.9/0.10 (`UserIdMap`, `1001..1999`, `2001..2999`,
  `synthetic_user_ids`, `synthetic_project_ids`, `trusted_declaration`,
  the sequence over a hash, keyed by `(forge, number)`, one map per
  recording and one per corpus offline). 0.17 changes one thing: a known
  id standing in free text (a branch `fix/user-<id>`, a GraphQL error
  citing a node id, `SP-<id>`, `project <id> was archived`) is replaced
  by its synthetic form (`fix/user-1001`, `U_1001`, `SP-1001`, `project
  2001`) instead of refused (rows `reg_branch_with_*_id`,
  `reg_node_*_in_url_path`, `reg_ci_log_graphql_*`,
  `reg_invented_sp_uid_literal_refused`, `uid_*`, `pid_*`); the 0.9
  refusal survives only for an embedded form (D3).
- D2. **Logins are classified at record time** — unchanged from 0.16:
  `build_roles` precedence (the configured bot, the `is_maintainer`
  login, the change request's author — so a Dependabot pull request
  recorded with its author names `dependabot[bot]` `author` and
  `@dependabot` `@author`, row `r10_o28` — then `author-<n>`, `bot-<n>`,
  `service`), `NON_PERSON_LOGINS`, `MENTION_RE` as a finder of unknown
  handles (a mention-only login replaced in its `@` form only, its bare
  word refused; 0.20: a mention-only handle shorter than
  `NEEDLE_MIN_LENGTH` is replaced in its `@` form and is no value, so
  `cc @bob` never refuses the recording), the language keywords after `@`
  (0.20, `LANGUAGE_AT_WORDS`, casefolded: `PYTHON_DECORATORS`, the
  completed `CSS_AT_RULES` of the CSS, PostCSS, Sass, Less and Tailwind
  specifications, `DOC_TAGS` — JSDoc, TSDoc, Javadoc, PHPDoc block tags
  and Doxygen commands — and `LANGUAGE_ANNOTATIONS` — Java, Kotlin, Swift
  and Dart with their standard libraries; a framework's annotation such
  as `@Autowired` or `@Test` is not a language word and gets a class
  word, the documented residual of the finder), the userinfo characters
  `:${}` blocking a mention before `@` like a letter does
  (`${TOKEN}@host` is a credential, 0.20), scoped packages, `[bot]` stems
  (`bot_mention_stem`, `bot_stem`), the common-word login replaced
  everywhere, `CLI_PLACEHOLDER_RE` literals (`:owner`, `:repo`, `:id`,
  `/dev/null`, `SPEC_IDS`, the non-person logins, `docs.github.com`,
  `docs.gitlab.com`). The token boundary is D7's.
- D4 (rewritten 0.17). **The offline transform is the live pipeline minus
  the forge.** `redact_fixture(data, ids=None, *, forge, projects=None,
  remote="", denylist=()) -> dict` in `scripts/redact_forge_fixtures.py`
  builds `K` with `known_values` from the fixture's own structured
  fields (its exchanges, `args`, `kwargs`, `bot_username`), the `remote`
  it is given (the CLI passes none; the acceptance test passes the row's)
  and the `denylist`, runs the same `redact_payload`, convenience
  redaction, `replace_known`, id synthesis and `legacy_roles`
  classification, and judges the result with the same
  `refuse_unclassified` (`ResidualValueError`, `<reason> at
  <file>:<path>` from `run()`, exit 2, nothing written, the whole corpus
  computed before any file is written). Parity, pinned by the acceptance
  test's offline leg: for every written row, `redact_fixture(live
  fixture)` is the live fixture (a fixed point); for every row whose raw
  text holds no value only the live recorder learns (every login under a
  `login` or `username` key, including the logins of `assignees` and
  `mergedBy` that the redaction drops, and their `[bot]` stems; each word
  of a name; a mailbox, its local part and its host; every `id` with its
  node forms, a repository's included; the author), the raw text put back
  into the live fixture's structured frame transforms to exactly the live
  fixture; for every refused row of that kind, the raw recording is
  refused with the same reason prefix. 0.18: the offline transform also
  gives the logins under dropped keys a class word, as `raw_logins` does
  live (`legacy_roles(data, also=raw_logins(exchanges))`: `args` and `kwargs` keep the keys the redaction drops, so `legacy_roles` walks `kwargs` itself, and a login in the members of `args`, which are never renamed, is refused; a committed fixture has
  no such key left, so the counterpart concerns a raw recording only). `--check` and the 0.9
  reproduction stand (C2: the committed fixtures are a fixed point and
  the rebuild from `7fd31af` is byte-identical).
- D5 (0.8) and D6 (0.9) — unchanged: the placeholders derived from a
  real remote in the tests, the deny-list test's file route, the CI
  secret wiring, the posture page (`docs/security_posture.md`, eleventh
  commit at 0.17, stating D7, D8, D9 and the deletion list).

## What 0.17 removes or demotes

The implementer deletes these from `src/repoach/forge/recording.py`
unless marked *demote*, with their tests, and names each deletion in the
commit that makes it (one commit per class):

| Class | Symbols | Why |
|---|---|---|
| protocol-relative grammar | `_PROTO_REL_LEAD_RE`, `_PROTO_REL_BODY`, `_PROTO_REL_IN_TEXT_RE`, `_PROTO_REL_WHOLE_RE`, `_redact_proto_rel`, `_unredacted_proto_rel`, `_is_slash_led`, `_TEXT_VALUE_LEAVES`, `is_structured_json_path`, `_JSON_VALUE_OPENER_CLASS`, `_QUOTE_CLASS`, `_HSPACE_CLASS` | R10-D1: the lead grammar is the leak; D7 replaces known values in every context |
| `glab api` tokeniser | `_GLAB_API_CMD_RE`, `_GLAB_API_FALLBACK_ID_RE`, `_GLAB_API_VALUE_FLAGS`, `_GLAB_API_ARG_PROJECT_ID_RE`, `_is_glab_api_argv`, `_glab_api_endpoint`, `_glab_api_text_id_spans`, `_API_PROJECT_ID_RE`, `_API_VERSION_RE`'s role in ids, `_api_project_id_spans`, `_kept_url_spans`, `_is_real_api_project_id`, `shlex` | R10 WF5, Preference 2: one rule, D9 (`_PROJECT_ID_RE`) |
| host reader | `locations`, `Location`, `remote_location` (*demote*: a parser of the `origin` remote only, D7 source 1), `_HOST_TOKEN_RE`, `_AFTER_HOST_DELIMITERS`, `_AFTER_HOST_CHAR`, `_AFTER_HOST_NOSLASH`, `_AFTER_HOST_SEG`, `_AFTER_HOST_RE`, `_URL_USERINFO_BEFORE_RE`, `_USERINFO_AHEAD_RE`, `_USER_BEFORE_RE`, `_has_user_before`, `_IPV4_AFTER_BLOCK_CLASS`, `_IPV4_LED_HOST_RE`, `_is_ipv4_host`, `_raw_url_host` | superseded by `closure.hosts` (D8), the one reader for `K` source 3 and for the oracle |
| needle builder | `needles`, `Needles`, `NEEDLE_KINDS`, `is_public_forge_host` (*demote*: used by D7 source 1), `PUBLIC_FORGE_DOMAINS` (*keep*) | renamed and rebuilt as `known_values` / `Known` (D7) |
| oracle forms | `oracle_forms`, `_plain`, `_decoded_once`, `CONFUSABLES`, `_JSON_U_ESCAPE_RE`, `_DECODE_ROUNDS`, `residual`, `embedded_id_numbers`, `_NEW_NODE_IN_TEXT_RE`, `_LEGACY_NODE_IN_TEXT_RE`, `_LEGACY_USER_NODE_PREFIX_RE`, `_legacy_node_number`, `_NODE_SUFFIX_MAX`, `_LEGACY_NODE_MAX` | re-implemented in `closure.py` (decoders, `node_id_numbers`, `residual`) without NFKC/`Cf`/confusables (NG4-4) |
| refusal helpers | `unredacted_host`, `_SYSTEM_NOREPLY_RE` (*demote*: convenience) | D3 judges hosts through the oracle |
| recorder script | `IPV4_RE`, `IPV4_RESERVED`, `IPV4_VERSION_BEFORE_RE`, `IPV4_VERSION_AFTER`, `_public_ipv4`, `MAILBOX_RE`, `MAILBOX_DOC_DOMAIN_RE`, `_SCP_AFTER_MAILBOX_RE`, `_personal_mailbox`, `URL_USERINFO_RE`, the `mailbox` and `public_ipv4` branches of `generic_class`; `find_secrets`'s use in `record()` | moved to `closure.py` as `IPV4_RESERVED`, `is_doc_mailbox_host` and the mailbox rule (D8); `find_secret_tokens` (D3) |
| kept-host exceptions | the kept-URL span exemption and the `< 4 digits` rule of project ids | D9: one threshold, five digits |
| character-class helper | `_char_class` (*demote*: a helper, no longer a claim) | 0.16 WF3 |

Demoted to convenience, kept: `redact_url`, `redact_urls_in_text`,
`redact_urls_in_value`, `_URL_IN_TEXT_RE`, `_SCP_IN_TEXT_RE`,
`_split_url_tail`, `_strip_balanced_tail`, `_normalize_kept_url`,
`_NOREPLY_MAILBOX_RE`, `_SYSTEM_NOREPLY_RE`, `_GITHUB_ROUTES`,
`path_identity` (also a `K` reader), `_RFC3986_*` and
`_URL_USERINFO_PREFIX` (extended by `${}`), `MENTION_RE`, `mentions`,
`bot_mention_stem`, `bot_stem`, `classify_text`, `classify_logins`,
`CLI_PLACEHOLDER_RE`, `PUBLIC_REFERENCE_HOSTS` (now read through
`closure.is_public_host`), `_EXAMPLE_HOST_RE` (now
`closure.is_placeholder_host`). Tests retired with their subject, each
named in the deleting commit:
`test_hosts_are_found_without_the_redactors_regexes`,
`test_locations_uses_positive_host_rule_and_scheme_led_ipv4`,
`test_protocol_relative_urls_require_positive_url_context_and_leave_code_comments_intact`,
`test_protocol_relative_body_context_and_value_rules_by_construction`,
`test_glab_api_shlex_parsing_quoted_flags_argv_and_unbalanced_fallback`,
`test_glab_api_command_recognition_flag_table_and_span_rules_by_construction`,
`test_api_project_ids_skip_doc_examples_and_cover_all_positions`,
`test_api_project_ids_ignore_year_paths_and_support_api_versions`,
`test_web_projects_identity_distinguishes_numeric_ids_from_names_by_construction`,
`test_the_oracle_judges_encoded_and_lookalike_forms` (its confusable
half), `test_residual_needles_are_seen_in_every_generated_form` (rewritten
over `closure.residual`), `test_segments_come_from_the_recorded_project_and_any_private_host_it_cannot_tell`
(rewritten over `known_values`), `test_round8_guard_invariants_kill_surviving_mutants`
(the `unredacted_host` assertions), `test_case_insensitive_patterns_are_flagged`
(extended to `closure.py`). Every other test of
`tests/unit/test_forge_recording_redaction.py` stays green unchanged or
with its expected text updated to a corpus row's, never loosened.

## End state

`scripts/record_forge_fixture.py` run against a real pull request writes
a fixture in which every known private value of the recording (D7) has
been replaced by its class token in every string, every `login`/
`username` is a class word or a non-person login, no personal key exists,
every user and repository id is a declared synthetic number, every URL
and remote the convenience layer recognises carries the placeholder host,
namespace and repository with its shape kept, and no string holds a host
that is neither a placeholder nor public, nor a public address, nor a
mailbox that is neither a placeholder nor a documentation literal, each
read in its encoded forms (percent, entity and `\uXXXX`, four rounds) as
well as written (0.18; at 0.17 only a known value was; 0.20: the
string-escape layer `\uXXXX`, `\xHH`, `\/` included), and in which the
oracle raised on nothing, a recording nested past the readers was refused
(`nesting too deep`, 0.21) rather than crashed on, and ordinary text — a
version, a diagnostic, a documentation tag, a mailbox on a placeholder
address, a path after a placeholder host, a lone page segment — is
byte-identical; or it exits 2 naming the reason and the position (D3). A
third party's private host cited bare in prose, two-label hosts un-led, a
private address bare or followed, a public IPv4 address cited bare or as a
path segment, an IPv6 address bare or un-led, a lone path segment after a
host, a path after a placeholder-class host, a mention of a
non-participant whose login is a language word, an identity after a
reserved route word, a third party's npm scope on a registry host
that is not a GitLab instance and an API path cited in prose are the named residuals (NG4-2, NG4-3,
NG4-8, NG4-9, NG4-10, NG4-11, NG4-12, NG4-13, NG4-14, NG4-15, NG4-16). The offline transform
does the same with the same functions (D4). The acceptance corpus is
green live and offline, the class sweep (AC8) finds no leak outside the
residuals and no change of ordinary text outside the documented
rewrites, the oracle is independent by test, and a mutation
run on `closure.py` leaves no non-equivalent survivor.

## Easy solution rejected

(0.17) Another round of shape fixes: rejected by the owner after ten —
each round fixed the shapes found and the next round found new ones, and
round 10 regressed round 9's. A TLD list alone for host shape: rejected
by the ordinary corpus — `config.dev`, `stream.io`, `ssl.ca`, `file.py`,
`README.md`, `run.sh` all end in a real TLD, so a two-label rule refuses
ordinary code; the rule needs a lead or a follow and the third label (D8, 0.18). Requiring
a scheme or `@` for every host: rejected by round 10 — `const CDN =
'//code.kvncloud.io/…'` and YAML `cdn: //…` have neither. Refusing every
`//host` lead as a URL: rejected — Go, JS, TS, C and Java comments
(`x := 1 //cfg.dev/env/prod`) are ordinary text (rows `r10_o14`–`r10_o20`).
The earlier rejections (0.6–0.16: hand rewrites, dropping `login`,
`sed`, dropping URL keys, dropping or hashing ids, magnitude thresholds)
stand.

## Hard part

(0.17) The same string is a host in one text and an identifier in
another, and no grammar of the surrounding text decides it reliably
(ten rounds measured that). What decides it is what the recording knows:
a value seen in a structured field is private wherever it reappears, so
it is replaced as a token everywhere; what remains for the oracle is only
the host-shaped, address-shaped and mailbox-shaped tokens the recording
never saw structurally, and for those the rule is one list and one
lead, enumerable and refusing by default. The second hard part is to keep
the convenience layer from undoing the guarantee: it runs first, D7 runs
after it, and the oracle judges last with code it shares with neither.
The earlier hard parts (stable class words, `name` by context, synthetic
ids declared) stand.

## Goals

- G1–G5: as at 0.16 (keys dropped by name and context, ids synthetic and declared, URL shape kept, logins classified with a stable mapping, the recorder refuses what it cannot classify, no real-remote string in the tests, the fixtures a fixed point).
- G6 (0.17): fail-closed — no private value of a recording can reach a fixture through an unrecognised shape; what the pipeline does not recognise, the oracle refuses by a rule stated by construction (D3, D7, D8), and the acceptance corpus holds the whole behaviour (AC1).

## Non-Goals

- NG1 (0.6): re-recording the existing fixtures live; they are transformed offline (D4).
- NG2: the generic lint (SP-NO-PERSONAL-DATA-GATE); this spec copies two things from it, `HOME_PATH_RE` and the documentation-domain list of mailboxes, and does not run the gate.
- NG3: redacting secrets (F2 and the forge-token gate of D3 do it already).
- NG4 (rewritten 0.17): accepted residuals under the **accidental-leak** threat model, each one a corpus row or a documented absence:
  1. A login the recording never learns — cited only as a bare word, under no login key and with no `@`. The maintainer's own accounts are in the deny-list; anyone else's is trimmed (`--trim`) or another change request is recorded. Rows `r10_p03_pip_index_token_private_registry` (a third party's `qwzpayments` with `remote=""`, written; its sibling `r10_p03b` with the remote known, replaced). (0.19) Key shapes the recorder does not read a login from are unreached the same way: a `user` object with a `name` only (no `login`, no mailbox), the keys `LOGIN`, `user_login` and `author_login`, and a login under a `login` key inside a JSON string of a text position (`{"assignee": {"login": "x"}}` in a body) (rows `v18_name_only_user_NG4_1`, `v18_login_upper_key_NG4_1`); no forge payload carries them. (0.20) The bare word of a mention-only handle shorter than `NEEDLE_MIN_LENGTH` (`cc @bob, as bob said`: `@bob` is replaced, `bob` is written) is unreached the same way, too short to be a token (row `v20_short_handle_at_form_replaced_bare_word_kept`; named test `test_a_short_mention_only_handle_is_replaced_in_its_at_form_and_never_refuses`).
  2. A **bare or followed** private, loopback or reserved IPv4 address, wherever it stands (`ssh: connect to host 10.20.30.40 port 22`, `curl 10.0.0.12:8080/metrics`, `10.1.2.3/<ns>/<repo>`): ordinary text, written as it stands, its path never read (rows `g_ipv4_private_bare_prose_written`, `v17_private_ipv4_path_followed_NG4_2`, `v18_followed_private_ipv4_port_path_NG4_2`, `v20_followed_private_ipv4_*`; 0.20, the orchestrator's correction of the 0.19 rule). A led one is a host: replaced, its namespace and repository read (row `v20_led_private_ipv4_reads_namespace`). The IPv6 counterpart is NG4-10.
  3. A **two-label** host that stands only un-led or protocol-relative and in no structured field (`zqcorp.io/ns/repo` in prose, `//zqcorp.io/…`): indistinguishable from a code token by construction (D8 H2 needs three labels), written as is (row `code_two_label_apex_host_schemeless_prose_residual`); the same host scheme-led, `@`-led or in any `URL_KEYS` value or the remote is a known value and is replaced everywhere (row `code_two_label_apex_host_scheme_led`). Likewise a host whose TLD is in `EXTENSION_TLDS`, un-led.
  4. Adversarial evasion forms (0.12, unchanged in scope; the 0.11 confusable and invisible-character pass is dropped from the oracle): Unicode confusables and invisible characters (a fullwidth solidus, a zero-width split), Markdown-split or escaped values, non-node-id base64, percent, entity or string escapes beyond four decoding rounds. A finding of these forms is NG4, not a defect. (0.20) The JSON escape `\/` and the hex escape `\xHH` are **not** of this class: they are a decoded layer like `\uXXXX`, so `http:\/\/<host>\/<ns>` is replaced or refused (row `v20_escaped_slash_*`; named test `test_json_and_hex_string_escapes_are_a_decoded_layer`).
  5. A mailbox the convenience layer turns into `<class word>@forge.example.com` is refused, not written (D3 accepted refusals): the author of a recording that quotes a personal mailbox trims it.
  6. A project id of fewer than five digits cited in text is written as is (D9; `projects/1234` on a self-hosted instance with fewer than 10 000 projects); under an `id` key it is synthetic whatever its size (D1c).
  7. A name word shorter than `NEEDLE_MIN_LENGTH` (`Jo`, `Li`) is not a value (D7).
  8. (0.18, owner decision D1) A third party's private host cited **bare in prose** — a dotted name with no scheme, no `<local>@`, no `//` before it and no `/<segment>` or `:<port>` after it — is ordinary text, whatever its last label, and is written as is (`Host mirror.zqcorp.net is down`, row `code_three_label_private_host_schemeless_no_path`, test `test_a_bare_dotted_host_in_prose_is_the_documented_residual`); led or followed it is a host and is refused or replaced, and a host known from a structured field is replaced wherever it stands. The price of not corrupting `response.data.id` and `self.logger.info`.
  9. (0.19, owner decision; 0.20) A public IPv4 address cited **bare, wherever it stands** — prose, a JSON or YAML value (`{"host": "104.16.0.1"}`), a list — or standing as a **path segment** of a URL (`ipinfo.io/104.16.0.1/json`: a single `/` before a quad is a version context, D8) is ordinary text, whatever its octets, and is written as is: it cannot be told from a version (`1.8.0.252`, `ASA 9.12.4.1`, `pkg = 1.2.3.4`, `/ojdbc8/19.3.0.0/`) by its digits. Led by a scheme, `<local>@` or `//`, or followed by `:<port>` or `/<segment>`, it is a host (`104.16.0.1:8080` and `104.16.0.1/kvnteam` are refused). Rows `g_ipv4_public_bare_prose_written_NG4_9`, `v18_bare_public_ipv4_in_prose_NG4_9`, `v20_public_ipv4_as_path_segment_NG4_9`; named tests `test_a_bare_dotted_quad_in_prose_is_ordinary_text_whatever_its_octets`, `test_a_quad_after_a_single_slash_is_a_version_and_a_comparator_allows_spaces`.
  10. (0.19) An **unbracketed** IPv6 address (`ping 2a00:1450:4001:80b::200e`) and a bracketed literal that has a path but no lead (`see [fd00::1]/ns/repo`, `[fd00::1]:8080/ns/repo`) are ordinary text, as a bare IPv4 address is (NG4-2, NG4-9): the address is written and the namespace and repository after an un-led literal are not read; a bracketed global literal alone is a `public address`, and a led one is a host. Rows `v18_v04_unbracketed_global_unled`, `v18_v13_ipv6_unled_ula_path_NG4_10`; named test `test_a_bare_or_un_led_ipv6_address_is_the_documented_residual`.
  11. (0.20, decided under the owner's goal; 0.21: the rule applies to every host — the recorded instance, a public forge, a third party's — this line said "a third-party host") A **lone path segment** after a host in a non-URL lead (`//code.kvncloud.io/kvnteam`, `git@host:kvnteam`) is a page, not an identity, and is written; a text path names someone only with both a namespace and a repository or through an API root (D7 source 3). The price of not learning `docs`, `health`, `metrics` or `explore` as a namespace and rewriting that word everywhere. Rows `v20_lone_segment_*`; named test `test_a_lone_path_segment_after_a_host_is_a_page_not_a_namespace`.
  12. (0.20, decided under the owner's goal) A third party's namespace and repository after a **placeholder-class or public host** (`http://localhost/<ns>/<repo>`, `http://[2001:db8::1]/<ns>/<repo>`, `//www.example.com/<ns>/<repo>`) are not read: the host names no one, and the recorded project's own namespace and repository are known values replaced wherever they stand. The price of not learning `api` and `users` from `http://localhost:3000/api/users`. Rows `v17_ipv6_doc_url_path_NG4_12`, `v20_ipv6_doc_upper_padded_path_NG4_12`, `v20_localhost_api_path_not_read`; named test `test_nothing_is_read_after_a_placeholder_class_host`.
  13. (0.21, decided under the owner's goal) A **mention of a non-participant whose login is a language word** — one of the 425 `LANGUAGE_AT_WORDS` of D2 (`cc @test please review`, `ping @main`, `fix: @link reported this`) — is written in its `@` form: the finder cannot tell the handle from the word by construction, and narrowing the skip set to a syntactic context would rewrite ordinary prose about doc tags and pasted code (`cc @param please`, `completion: @escaping`) and would not close a line-leading mention. A participant's login equal to a language word is a known value, replaced wherever it stands and refused when it survives. Rows `v21_language_word_mention_only_NG4_13`, `v21_language_word_mention_only_in_title_NG4_13`, `v21_language_word_participant_login_replaced*`; named test `test_a_mention_only_handle_equal_to_a_language_word_is_the_documented_residual`; sweep `language_word_cells` (849 cells).
  14. (0.22, decided under the owner's goal and the owner's list) An **identity that follows a reserved route word** is not read: GitLab's legacy `s/<user>` redirect and its admin area (`admin/users/<u>`, `admin/groups/<g>`, `admin/projects/<ns>/<r>`), Bitbucket's `snippets/<ws>/<id>`, a segment after `.well-known`. A reserved word names no one, so the path led by one is written as it stands (`uploads/-/system/…`, `help/user/profile`); the price is the name an instance's own pages carry after one, which a reviewer pastes seldom and which the generic reading replaced at 0.21 while corrupting the word before it (`uploads` → `org`). Row `v22_gitlab_s_redirect_identity_after_a_reserved_word_NG4_14` (live, the identity after `s/` written on the instance; 0.23 — this item cited the ordinary rows `v22_*_reserved_word_*`, which plant no identity after the word); named test `test_a_reserved_route_word_is_never_an_identity` (the five grammars asserted); sweep `RESERVED_WORDS` (every forge's words in two shapes). The rule is a URL path's: the recorded remote and an argv API path read `<ns>/…/<repo>` by construction (0.23 (a), 0.24 (a) and (b)).
  15. (0.23, decided under the owner's goal; 0.24: every registry, the GitLab instance included) An **npm scope read from a URL** (`https://npm.<corp>/@<scope>/<pkg>/-/<pkg>-<v>.tgz`, a Nexus or Artifactory proxy, GitLab's own `api/vN/…/packages/npm/@<scope>/…` on the instance, project and group endpoints, and the company-scope case `npm.<corp>/@<corp>/…`) is never learnt and is written as it stands, the host replaced; a scope equal to a known namespace (the remote's) is replaced as that value. A scope is as often a forwarded or proxied public one (`@babel`, `@types`, which GitLab's package forwarding brings to its own endpoints, whose project and group forms impose no scope naming) as a corporate one, and reading it as a namespace corrupts every yarn.lock line and every import of the fixture's code; the price is a third party's or the company's own scope kept in a URL and beside it. Rows `v23_scoped_tarball_on_a_private_registry_keeps_its_scope_NG4_15`, the five `v23_gitlab_npm_scope_*_is_ordinary_text_NG4_15`, `v24_company_scope_on_its_own_registry_is_ordinary_text_NG4_15` (and the controls `v23_scoped_tarball_on_a_nexus_proxy_keeps_its_scope`, `v24_forwarded_types_scope_next_to_a_gitlab_npm_url_stays_in_code`, `v24_forwarded_babel_scope_on_a_gitlab_project_endpoint_stays_in_code`, `v24_gitlab_npm_scope_equal_to_the_remote_namespace_is_replaced`); named test `test_an_npm_scope_is_never_learnt_on_any_registry` (the ordinary shapes asserted).
  16. (0.27, the owner's decision of 2026-11-24, structure versus prose) An **API path cited in prose** — `[/][api/v<N>/]repos/<o>/<r>/<REST word>…` in a body, a merge request description, a code block, a review comment's `path`/`new_path`/`old_path`, a title, a commit message, a GitLab change's `diff`, a raw (non-JSON) stdout, or a channel the call declares prose (an argv `body=`/`path=`/`title=` field, the argument of `--body`/`--title`/`--message`/`--description`, a stdin declared by `--body-file -` or `-F body=@-`, a positional argument the method's signature names `body`; 0.28) — names no one and is written as it stands; the same path in the argv, stdin, stderr, a JSON stdout field or the method's arguments is read (D7, `PROVENANCE` row `structured`), whatever words follow it (0.28: the 0.27 cited-file sentence rule is withdrawn). Reading prose cost 74 corrupted ordinary directory cells at 0.26; the price is another repository named through the GitHub API in a comment, which SP-NO-PERSONAL-DATA-GATE and its optional NER oracle cover later. Rows `v27_body_led_by_a_github_api_path_is_prose_NG4_16`, `v27_json_string_in_a_body_is_prose_NG4_16`, `v27_review_path_field_api_path_is_prose_NG4_16`, `v27_reserved_owner_api_path_in_a_body_is_prose_NG4_16`, `v27_code_block_api_path_is_prose_NG4_16`, `v27_gitlab_body_api_path_is_prose_NG4_16`, `v27_raw_stdout_led_by_an_api_path_is_prose_NG4_16`; named test `test_an_api_path_cited_in_prose_is_the_named_residual_ng4_16`; the declared channels: `test_prose_is_declared_by_the_call_never_by_its_wording`, `test_a_prose_positional_argument_is_named_by_the_method_signature`, rows `v28_*`.

## Assumptions

- A1: SP-SCRUB-PERSONAL-DATA-AT-HEAD has merged (unchanged).
- A2: no forge reads `avatar_url`, `html_url`, `full_name`, `email`, or the `name` of a user object (unchanged; re-measured by the implementer beside AC1).
- A3 (0.8, 0.14, 0.17): SP-NO-PERSONAL-DATA-GATE has not landed; `HOME_PATH_RE` and the documentation-domain list are the two copies this spec keeps, held equal by that spec's tests once it lands. Open: that gate's exact `example.com` rule would flag the noreply placeholders on `users.noreply.forge.example.com` (Open Questions, Q5).

## Interface

Inputs (0.17; the 0.6–0.16 names not listed here are deleted or demoted per **What 0.17 removes or demotes**):
- `src/repoach/forge/closure.py`: `HOST_TLDS`, `EXTENSION_TLDS`, `REVERSE_DNS_HEADS`, `LOCAL_CHARS`, `IPV4_RESERVED`, `PUBLIC_SUFFIXES` (the 26 registrable suffixes), `is_placeholder_host(host)`, `is_public_host(host)`, `is_doc_mailbox_host(host)`, `Host(host, led, start, end, literal=False)`, `hosts(text) -> list[Host]`, `Mailbox(local, host, start, end)`, `mailboxes(text) -> list[Mailbox]`, `public_addresses(text) -> list[str]`, `token_pattern(word)`, `decoded_forms(text) -> list[str]`, `forms_of(text, *, literals=())`, `node_id_numbers(text) -> list[str]`, `is_literal_mailbox(box)`, `JUDGED_KINDS`, `Known` (an opaque, count-only repr; `values(kind)`), `residual(text, known) -> tuple[kind, n] | None`, `judge(text, known) -> Refusal | None` with `Refusal(reason, kind, n)` and reasons `residual`, `unknown host`, `public address`, `unknown mailbox`.
- `src/repoach/forge/recording.py`: `known_values(raw, *, roles, remote="", denylist=(), min_length=4, ids=None, projects=None, forge="github", keep_users=frozenset(), keep_projects=frozenset()) -> Known`, `replace_known(text, known, mapping=None) -> str`, `replace_known_in_value`, `replace_known_in_stdout`, `Known.mapping()`, `ReplaySpawn(exchanges, normalize=None)`, `find_secret_tokens(text)`, `SECRET_TOKEN_PATTERNS`, `_PROJECT_ID_RE`, `project_id_values` (D9), `_api_route(path)`, `LANGUAGE_AT_WORDS` with its parts `PYTHON_DECORATORS`, `CSS_AT_RULES`, `DOC_TAGS`, `LANGUAGE_ANNOTATIONS`, `_URL_IPV6_HOST` and `_URL_BODY` of `_URL_IN_TEXT_RE` (0.20), plus the unchanged 0.16 names of D1, D1b (demoted), D1c and D2.
- `scripts/record_forge_fixture.py`: `refuse_unclassified(fixture, denylist, known)`, `generic_class` (home path only), `build_roles`, `Denylist`, `read_denylist`, `origin_url`, `record`, the CLI unchanged.
- `scripts/redact_forge_fixtures.py`: `redact_fixture(data, ids=None, *, forge, projects=None, remote="", denylist=())`, `fixture_known(data, *, roles=None, remote="", denylist=(), ids=None, projects=None, forge="github")`, `ResidualValueError`, `run`, the CLI unchanged.

Outputs: a fixture file, or exit 2 with one of `unclassified: <path>`, `generic class home_path at <path>`, `deny-list entry <n> at <path>`, `deny-list entry <n>: too short`, `residual <kind> <n> at <path>` (kinds `login`, `host`, `segment`, `id`, `name`, `mailbox`), `unknown host at <path>`, `public address at <path>`, `unknown mailbox at <path>`, `needle <kind> <n>: too short`, `needle <kind> <n>: a class word or placeholder`, `synthetic user id <n> past 1999`, `synthetic project id <n> past 2999`, `refusing to write a fixture carrying <n> secret-shaped value(s)`, `nesting too deep: the recording cannot be walked` (0.21, a `RecursionError` of any reader; offline `nesting too deep: the fixture cannot be walked`) on stderr, stdout empty. `generic class mailbox`, `generic class public_ipv4` and `unredacted host` are no longer written by anything.

Errors: `UnclassifiedValueError`, `DenylistError`, `UnknownForgeError`, `NeedleError`, `ResidualValueError`, `SyntheticIdsExhaustedError` as at 0.16; every message carries a kind, an ordinal or a path, never a value.

## Behavior

### Nominal

The implementer writes `closure.py` and its enumeration tests first, then
`known_values`/`replace_known`, then rewires `refuse_unclassified` and
`redact_fixture`, then deletes the table above class by class, removes
the `xfail` mark from the acceptance corpus, and runs it live and
offline; every row that differs is a defect of the implementation, never
a row to edit. One recording against PR #316 (C3) closes the pass.

### Edge cases

Every edge case of 0.8–0.16 is a row of the acceptance corpus or of
`tests/unit/test_forge_recording_regression_corpus.py` (folded into the
acceptance corpus and deleted in the 0.17 pass: its rows are the
acceptance rows prefixed `reg_`). New at
0.17, each a row: a private host in a quoted or spaced assignment in Go,
JS, TS, Python, TOML, YAML (single-quoted or bare), PHP, Ruby, CSS
`url( … )`, a JS template literal, an HTML attribute with spaces, a diff
line (`r10_a_proto_rel_*`: written with the placeholder, path kept); the
same host also cited with `https://` (`r10_a2_*`); `insteadOf` blocks
and `GOPRIVATE` lines (`r10_ci_*`, `r10_b_*`: written, `git@forge.example.com:`
kept); `${CI_JOB_TOKEN}` and `$GITLAB_TOKEN` userinfo (written, the
userinfo gone); a `glab api` line with an apostrophe, two ids, an
unbalanced quote, `%2F` (`r10_c_*`, `r10_d_*`, `r10_i1_d4_*`: ids `2001`,
`2002`, segments `org%2Fproject`); a three-label schemeless private host
in prose with a path or a port (`code_three_label_private_host_*`:
replaced) and without either (written, NG4-8); a two-label apex host un-led (written, NG4-3) and scheme-led
(replaced); three-label file names and reverse-DNS package names
(unchanged); `files.pythonhosted.org`, `cdnjs.cloudflare.com`,
`cdn.jsdelivr.net`, `www.mend.io`, `docs.renovatebot.com` (kept); a user
`name` and `email` under dropped keys cited in prose (replaced by the
class word) and a personal mailbox in prose (`unknown mailbox`); a known
id in prose (synthetic); every position with a URL, an SCP remote, a
login and a deny-list entry (`pos_*`, `deny_in_*`).

### Failure scenarios

- A value not in `roles` → exit 2, `unclassified: exchanges[3].stdout.author.login`.
- A home path → exit 2, `generic class home_path at <path>`.
- A deny-list entry, a node form of a numeric one included → exit 2, `deny-list entry <n> at <path>`.
- A known value the replacer missed (an encoded form, a key, a mention-only bare word, an embedded node id) → exit 2, `residual <kind> <n> at <path>`.
- A host that is led or followed (D8) and is neither a placeholder nor public, after replacement, in any decoded form → exit 2, `unknown host at <path>` (only reachable when D7 missed it: the oracle's reader is also `K`'s source 3, so a mutation of `replace_known` is what this catches).
- A public IPv4 address that is followed (`:<port>` or `/<segment>`) and not led, outside a version context, or an un-led global IPv6 literal, in any decoded form (`\/` and `\xHH` included) → exit 2, `public address at <path>` (0.19, 0.20; a bare public quad, or one that is a path segment, is NG4-9).
- An input that once made the oracle raise (`[::ffff:01.2.3.4]`, `U_` + 5 000 digits) → judged like any other (0.20, C6); the oracle raises nothing: `tmp/fable020/crash_probe.py` and `test_the_oracle_never_raises_on_a_mapped_tail_or_a_long_digit_run`.
- A personal mailbox, a credential userinfo the convenience layer left → exit 2, `unknown mailbox at <path>`.
- The deny-list is absent → judged with none; the fixture test skips, naming the reason (unchanged).

## Tests

| Test | What it pins | Mutation that must turn it red |
|---|---|---|
| `tests/unit/test_forge_recording_acceptance_corpus.py::test_acceptance_corpus_live_and_offline` (0.19, 752 rows) and `::test_the_planted_and_ordinary_split_is_computed_from_the_rows` (0.19: the split of AC1 comes from the file, a residual is a written unmarked row) | AC1: every row's one outcome through `record()` against a fake forge and through `redact_fixture` with the row's remote and deny-list; markers never survive; exact text, title and named fields; offline parity and fixed point | any change of D1b, D2, D3, D7, D8, D9 or D4 that alters one row's text or reason; measured 2026-11-22 on `95ccaf9`: 188 rows red live, every row red offline |
| `tests/unit/test_forge_closure.py::test_the_oracle_shares_no_code_object_with_the_redactor` (0.17) | AC2, D3 independence: no shared `re.Pattern`/function/class/frozenset object, no import, no equal pattern source | import anything from `recording` into `closure`, or alias one pattern |
| `::test_host_rule_tld_lists_reverse_dns_heads_and_extension_codes_by_enumeration` (0.17) | D8: `HOST_TLDS` membership and shape, `EXTENSION_TLDS` absent, `REVERSE_DNS_HEADS`, H1 for every scheme and `<local>@` lead, H2 only with a third label, a listed TLD and a lead or a follow, the 128 ASCII delimiters | add or remove one code, accept two labels un-led, accept a `//` lead as H1 |
| `::test_ipv4_rule_reserved_table_version_lead_and_led_addresses_by_enumeration` (0.17, 0.19) | D8 IPv4: every `IPV4_RESERVED` network, every version lead and archive tail, a quad led or followed is a host and public is refused, a bare quad is no host, the four-part versions | drop one network, drop one lead, restore the zero-octet exception |
| `::test_a_bare_dotted_quad_in_prose_is_ordinary_text_whatever_its_octets` (0.19) | NG4-9: versions, DNS addresses and bare public addresses written; `104.16.0.1:8080` and `/kvnteam` refused | refuse a bare quad, drop the follow |
| `::test_an_ipv6_zone_id_is_part_of_the_literal_and_an_ipv4_tail_is_read` (0.19, 0.21) | D8 IPv6: `%<zone>`, `%25<zone>`, `::ffff:a.b.c.d`; 0.21: the 45-character fully expanded mapped literal is a host led and a public address un-led | stop the bracket at `%`, drop the dotted tail, shorten `_BRACKETED` (M21) |
| `::test_a_bare_or_un_led_ipv6_address_is_the_documented_residual` (0.19) | NG4-10: unbracketed and un-led literals written, a bracketed global literal refused | read a bare group run as a host |
| `::test_a_known_public_suffix_is_a_public_host_and_apache_is_one` (0.19) | D8: `apache.org` and its subdomains, not `notapache.org` | drop the suffix, match a substring |
| `::test_the_judge_blanks_the_literals_before_it_decodes` (0.19) | C25: `x@docs.github.com`, `x@docs.gitlab.com` written | decode before blanking |
| `::test_mailbox_rule_literals_credentials_and_doc_domains_by_enumeration` (0.17) | D8: the four placeholder mailboxes, `git@<placeholder-class host>`, every documentation domain, a `:` credential, a subdomain of `example.com`, `LOCAL_CHARS` incl. `${}` | widen `LOCAL_CHARS`, accept a credential, accept `forge.example.com` as a doc domain |
| `::test_public_allowlist_and_placeholder_class_by_enumeration` (0.17) | D8: the 25 suffixes exact and as `.<suffix>`, trailing dot, case, public forge hosts never public, RFC 5737 and `*.example.com` placeholder-class | add `github.com`, drop one suffix |
| `::test_residual_judges_known_values_in_decoded_forms_and_embedded_node_ids` (0.17) | D3: four decoding rounds, casefold, `%2F` delimiter, padded node ids, embedded legacy and `U_` node ids, kind order and ordinals | drop a round, drop the padding, widen the boundary |
| `::test_refusal_reasons_name_a_kind_and_a_position_never_a_value` (0.17) | D3: every `Refusal` and every `UnclassifiedValueError` message of the recorder under `pytest -l` holds no value of `K` and no deny entry | print a value |
| `tests/unit/test_forge_recording_redaction.py::test_known_values_are_replaced_as_tokens_in_every_position_by_construction` (0.17) | D7: every source, every kind, every position, `%2F`, padding, longest first, mention-only logins untouched in bare form | drop one source, one kind or one position |
| `::test_project_ids_are_five_digit_projects_tokens_in_every_position_by_construction` (0.17) | D9: four versus five digits, years 2020–2030, every position, no `project` placeholder for an id segment | lower the threshold, skip a position |
| `::test_record_refuses_in_the_fixed_order_and_keeps_only_the_token_secret_gate` (0.17) | D3 order, `find_secret_tokens` (a mailbox is not a secret, a `ghp_` token is) | reorder, restore the mailbox pattern |
| `::test_userinfo_class_accepts_shell_variables` (0.17) | D1b: `${VAR}` and `$VAR` userinfo stripped, no corruption | drop `${}` from the class |
| `::test_offline_transform_takes_the_remote_and_the_denylist_and_matches_the_recorder` (0.17) | D4: `redact_fixture(remote=, denylist=)` builds the same `K` as `record()` on the same exchanges | ignore `remote` |
| `tests/unit/test_forge_closure.py::test_a_dotted_name_is_a_host_only_when_led_or_followed_by_construction` (0.18) | D8: every lead and every follow, the segment-start characters and the port digits one by one, the ordinary identifiers that are never hosts | widen the follow, drop the `//` lead, let a bare name through |
| `::test_a_bare_dotted_host_in_prose_is_the_documented_residual` (0.18) | NG4-8: bare, written; known, residual; followed, unknown host | make a bare name a host |
| `::test_a_percent_escape_is_never_part_of_a_label_run` (0.18) | D2: `%2F` and `%2f` before a run, one hex digit, a non-hex digit | read the escape as a label |
| `::test_the_judge_reads_hosts_addresses_and_mailboxes_in_every_decoded_form` (0.18) | D2: encoded lead, segment, `@` and dots refused, encoded placeholders kept | judge the raw form only |
| `::test_the_legacy_node_reader_never_raises_on_a_malformed_run` (0.18) | D3: every length and padding of a `MDQ6VXNl` run | pad twice |
| `::test_a_bracketed_ipv6_literal_is_judged_like_an_ipv4_address` (0.18) | D8 IPv6: led, un-led, slices, documentation prefixes, the parser's accept and reject table | accept a slice, drop a prefix |
| `::test_the_version_guard_of_a_led_address_is_observable` (0.18) | H07: a led address before `-` or `+` is a version | drop the guard |
| `tests/unit/test_forge_recording_redaction.py::test_known_values_read_encoded_forms_and_replace_them_without_a_stray_percent` (0.18) | D2, D7 source 3: encoded host, namespace and repository learnt, replaced whole | read the raw form only |
| `::test_known_values_read_bracketed_ipv6_hosts_and_the_names_of_git_authors` (0.18) | D8, D5: namespace and repository after an IPv6 literal, a git author's name words | drop the `literal` follow, require a login |
| `::test_css_at_rules_are_no_mention_and_the_other_handles_stay` (0.18) | D2: every at-rule keyword, upper-cased too | drop a keyword |
| `::test_offline_roles_read_the_logins_under_the_keys_the_redaction_drops` (0.18) | D4: `legacy_roles(also=)` | ignore `also` |
| `::test_known_values_read_the_path_after_a_led_address_only` (0.20, rewrites the 0.19 test; 0.21) | D8 IPv4, D7 source 3: the namespace and repository after a led quad, a five-digit port included; a followed quad is never learnt and its path never read; a followed public quad is refused | read the path after a followed address (R10), read four port digits (R01) |
| `::test_a_lone_path_segment_after_a_host_is_a_page_not_a_namespace` (0.20, 0.21) | C2, NG4-11: a lone segment is never a value, two segments and an API root are, on the instance too; 0.21 B1: the segments before a GitLab `/-/` (one, two, after `groups/` or `users/`) are, on the instance, gitlab.com (convenience layer only) and a third party, and `/-/jobs/5` names no one | learn a lone segment (R11), keep the segment before `/-/` |
| `::test_a_mention_only_handle_equal_to_a_language_word_is_the_documented_residual` (0.21) | NG4-13, B2: every `LANGUAGE_AT_WORDS` word is no mention in a body or a title; a participant login equal to one is a role, replaced and refused when it survives; a mention-only one is no value | drop a word from the skip set, learn a mention-only word as a login |
| `::test_the_recorder_never_raises_on_a_recording_nested_past_the_readers` (0.21) | D3, B3: 20 000 nested `[`, `[]` and `{}` decode to `None` and are text; a 990-deep value is refused `nesting too deep` offline; `record()` turns a reader's `RecursionError` into `UnclassifiedValueError` | catch the decode error only, drop the guard |
| `tests/unit/test_forge_recording_acceptance_corpus.py` rows `v21_*` (0.21) | B1 (seven route rows), NG4-13 (two residual rows, two participant rows), B3 (two written, two refused `nesting too deep`) | any of the three rules reverted |
| `::test_a_reserved_route_word_is_never_an_identity` (0.22) | D1b, D7 source 3, NG4-14: every word of `_RESERVED_ROUTE_WORDS` in two shapes, bare, with a trailing slash and in upper case names no one through `path_identity`, `redact_url` and `known_values`; the instance's own values still read beside one; `user` is not reserved; the five NG4-14 grammars | drop a word, compare case-sensitively, read the segment after a reserved word |
| `::test_a_web_ide_route_reads_its_project_path` (0.22) | D1b, D7 source 3: `-/ide/project/<ns>/<repo>/<ide word>/…` on every host and lead, subgroups, `.git`, a trailing slash, every IDE word in any case, the bare and the lone shapes, `_api_route` counts the route, `known_values` learns the segments, gitlab.com rewritten by the convenience layer only | stop at the wrong segment, read a non-IDE dash route, drop `_ide_route` from `_api_route` |
| `::test_an_owner_root_reads_the_identity_slots_of_its_grammar` (0.22) | D1b, D7 source 3: `_IDENTITY_SLOTS` by enumeration — `users/<u>/repos/<r>`, `projects/<KEY>/repos/<r>` (four and seven segments), `orgs/<o>/teams/<t>` and its API shape, a slot word of another root not read, the slot not read after a dash, `known_values` and `replace_known` through the route | empty the table, read a slot from the wrong root, write a team as a project |
| `::test_the_owner_root_stops_at_the_first_dash` (0.22) | D1b, the 0.21 survivor N14: after an owner root the owner ends at the first `-`, a second `-` is part of the route | read the last dash |
| `::test_a_package_tarball_before_a_dash_names_no_one` (0.22) | D1b, D7 source 3, D8: eight tarball shapes (scoped, Nexus prefix, prerelease, build metadata, a trailing slash) name no one, eight non-tarballs keep the generic reading, the two guards of `_package_tarball`, `yarnpkg.com` and `npmmirror.com` public, a tarball on a private host or a led address written with its host replaced and its path whole | drop the tarball check, loosen the version, drop a guard, drop a suffix |
| `::test_a_remote_path_names_a_repository_by_construction` (0.23; 0.24: the nested `uploads` remote written `org/project`) | D7 source 1, B1: every reserved word as a remote namespace yields it and the repository, with a subgroup, a `.git`, a lone segment, empty segments; the route roots as a remote namespace too; `known_values` learns the remote's values on GitHub, on the instance, from an https and an ssh remote, and replaces them in a route; a structured URL field keeps the reserved rule; a short namespace is `needle segment 1: too short` | apply the reserved rule to the remote, read the remote as a route, read a lone segment as a repository, read every path as a remote |
| `::test_every_hard_identity_path_reads_a_repository_path_by_construction` (0.24) | D7 sources 1 and 2, B1-a, B1-b, N13: a lone remote segment that is a reserved word is the namespace; a nested remote under `marketplace`, `uploads`, `settings`, `assets` learns each segment and the joined `<word>/<sub>`, written `org/project` after a `/-/` route, on the project and group pages and in a clone address, a fixed point of the text rewrite; a three-level namespace joins each leading path; a flat remote learns no joined value; the argv `repos/<word>/<r>`, `projects/<word>%2F<r>` and nested `api/v4/projects/<word>%2F<sub>%2F<r>` for seven words including the owner roots; a short argv namespace is `needle segment 1: too short`; a `URL_KEYS` value under a reserved word learns nothing | read the argv path by the route grammar, drop the joined values, join only the whole namespace, join for every source, send a lone remote segment to the route grammar |
| `::test_the_provenance_table_is_the_one_rule_of_what_names_an_identity` (0.26; 0.27: five rows) | D7: `PROVENANCE` enumerated; every `_segments` call of `known_values` inside the four readers, every `_add("segment", …)` inside them or `_segments`; `_api_project` once, as the argv source; `_structured_strings` once, as the structured source; `_api_path_projects` once, inside the `api_path` reader; the dispatch's source and reading names equal to the table's | a reading invoked outside the table, a row dropped, added or reordered |
| `::test_an_api_path_is_read_in_the_structured_sources_of_a_call_only` (0.27; 0.28: `title`, `message`, `messageHeadline`, `messageBody`, `diff` prose, `Title` structured) | D7, the owner's structure/prose decision: an API path in an argv `--input=`, kwargs, flat, nested and JSON-encoded method args, raw and JSON stdin, stderr, a JSON stdout field at any depth is learnt; in a body, a code block, a JSON string in a body, a review `path`, a `description`, `new_path`/`old_path`, a title, a commit message, a diff, a raw stdout, a prose key of the method's arguments, nothing; `_structured_strings` enumerated on five shapes | read prose, skip a structured source, drop the keys, read a raw stdout, walk no JSON |
| `::test_an_api_path_cited_in_prose_is_the_named_residual_ng4_16` (0.27; 0.28: the sentence rule withdrawn) | NG4-16: a body, a review path and a raw stdout written as they stand; inside a structured string the words after the path decide nothing: a file followed by a sentence, a comma, a colon, a newline, a digit, a one-character word, `?:` (verify027 W04–W09) is read as a call is | read prose, judge a structured string by its wording |
| `::test_prose_is_declared_by_the_call_never_by_its_wording` (0.28) | D7, the owner's second decision of 2026-11-24: `_declared_prose` on 30 argv shapes (`-f`/`-F`/`--field`/`--raw-field` with a prose key, separate, a prose value holding `=`, `-f<field>`, `-f=<field>`, `--field=<field>`; `--body`/`--title`/`--message`/`--description` next or joined; `--body-file -`, `--body-file=-`, `-F body=@-`, `--field=body=@-`; a non-prose key, `line=@-`, a field without `=`, a trailing flag, `--input -`, non-strings, look-alike flags); neither the argv nor the structured reader reads a declared value or stdin; an undeclared field, the argument after the prose one, an undeclared stdin and a dict that is not an exchange are read; `_argv_strings` and `_structured_strings` enumerated | read a declared channel, declare by wording, widen a flag to the rest of the argv, drop a form |
| `::test_a_prose_positional_argument_is_named_by_the_method_signature` (0.28) | D7: `_prose_parameters` from `protocol.Forge` (`post_comment` {1}, `reply` {2}, `upsert_marked_comment` {2}; none for `conversation_comments`, `create_pull_request`, `post_review`, `find_marked_comment`, a property, an unknown or malformed name); `known_values(method=…)` reads no prose positional argument and still reads the others and the kwargs; `_without_prose_arguments` on five shapes; the offline transform of `post_comment_inline.json` with a body and a review path under `repos/` writes both as they stand in the argv, the arguments and the answer (verify027 `repro_post.py`) | ignore the method, guess positions, blank the kwargs |
| `::test_the_prose_keys_are_the_audited_keys_matched_as_written` (0.28) | D7: `_PROSE_KEYS` is the audited ten; `Body`/`MESSAGE` structured (W15); a JSON stdin behind blanks (W13) and a JSON array stdin (W14) walked | casefold the keys, skip the JSON probe |
| `::test_the_decoded_forms_of_an_api_path_are_read_in_order` (0.28) | D7, W24: the raw form before its decoded forms, so the first reading keeps its class | forms reversed |
| `::test_the_api_path_reader_reads_a_whole_path_between_its_bounds` (0.27) | the 0.26 verifier's X01–X16: the eight lookbehind leads, a doubled `api/v3/`, `api/v10/`, a one-character owner, a space before the word, `pulls:`/`pulls;`, `iſsues`, a `.git` owner, `Settings`/`SETTINGS`, `kvn%5Fteam` | the lookbehind narrowed, the prefix repeated or one digit, a two-character owner, `lstrip("/ ")`, dots-only trim, `lower()`, `_bare(owner)`, a case-sensitive reserved check, `%` in the owner |
| `::test_inside_a_source_the_first_reading_in_recording_order_keeps_the_class` (0.27) | D7, X21, X22: two URL fields in one answer and in two exchanges, two stderr, method args then kwargs: the first reading keeps its class | a source's items reversed, URL fields prepended |
| `::test_a_url_field_on_a_single_label_host_learns_the_host` (0.27) | D7 source 2, X26: `http://intranet/…` in a URL field is a host value, in a body not | the route reader learns no host |
| `::test_a_github_api_path_names_a_repository_only_through_a_rest_route` (0.26 as `test_a_text_github_api_path_…`; 0.27: its known-values cases in a stderr) | D7, the 0.25 leak: `repos/<o>/<r>/<REST word>` learnt soft (leading `/`, `api/v3/`, doubled slashes, a `.git` suffix, a JSON-escaped form, a sentence end); a bare path, a file or directory after the repository, a path inside a longer path or a URL, CLI templates and placeholders learn nothing; a reserved, literal, numeric or short owner is not learnt and its repository is; the 74 words | no lookbehind, no `api/v3`, a bare path read, any route word, case-sensitive words, owner or repository dropped or kept unfiltered, hard, raw text only |
| `::test_every_exchange_argv_is_read_by_construction` (0.26) | D7 source 1, mutant B09: the argv of a second exchange is read by construction, in either order | walk the first member of a list |
| `::test_a_value_a_url_field_names_as_a_namespace_keeps_its_class` (0.26) | D7, mutant B19: the URL fields are read before the argv, so a value named both ways is `org` | read the argv before the URL fields |
| `::test_only_an_argv_string_is_read_as_an_api_path` (0.25) | D7 source 1, the 0.24 regression: a body led by `repos/public/…`, `repos/settings/…`, `projects/help%2F…` or `repos/frontend/…` and a review comment's `path` `repos/assets/…` learn no segment and are written as they stand; `_argv_strings` reads the `args` of an exchange wherever it stands, its strings only, never a list outside an exchange, a non-list `args` or a string's decoded JSON; the argv `repos/settings/<r>` and `projects/help%2F<r>` still read by construction | read every string through `_api_project` (0.24), keep the 0.23 text reading or read it soft, drop the argv loop, read argv by the route grammar or soft, require `program` or drop the `stdout` test, keep non-strings, walk no dict or list, decode strings, read one argv string |
| `::test_a_url_field_path_learns_no_joined_namespace` (0.25) | D7 source 2, mutant V09: a GitLab `web_url` under a nested group learns its segments one by one, no joined `<a>/<b>`, so a body `kvnteam/kvnsub-old` is written `org/org-old` | join the namespace for every hard source |
| `::test_an_npm_scope_is_never_learnt_on_any_registry` (0.24; 0.23: `test_a_gitlab_npm_scope_is_a_root_group`) | D1b, D7 source 3, B2-c, NG4-15: `_npm_registry` on the three endpoints, in any case, and seven negatives; twelve tails per endpoint written as they stand, the prefix's namespace only; the encoded project prefix; `known_values` and `replace_known` keep `@types/node` and `@babel/core` in code next to a GitLab npm URL on a third-party instance and a led address, read a dashless `groups/<g>/packages/npm/` prefix as the group alone, keep a third party's and a company's own scope, and replace a scope equal to the remote namespace | learn the scope again, write it `@org`, drop the registry check, read the prefix generically |
| `::test_an_owner_root_is_read_in_any_case` (0.23) | D1b: `repos`, `users`, `orgs`, `apps`, `groups` and `api/vN/` matched casefolded, the root word written as it stands, through `path_identity`, `redact_url` and `known_values` | compare a root raw, write the root lowercase |
| `tests/unit/test_forge_recording_acceptance_corpus.py` rows `v28_*` (0.28: eighteen ordinary rows, one per declared prose channel — argv `-f body=` (one with a value holding `=`, verify028 V28-06), `--raw-field=title=`, `-F path=`, the argument of `--body`, `--title`, `--message=`, GitLab `--description`, a stdin declared by `--body-file -` and by `-F body=@-`, the keys `title` (GitHub and GitLab), `message`, `messageHeadline`, `messageBody`, a GitLab `diff`, a kwargs `message`, a JSON stdin `title` —; five planted rows on the structured side — an undeclared stdin sentence, a stderr file followed by words, a stdin with an undeclared `@-` field, an undeclared argv field, the argument after the prose one) | 0.28 (a)–(c) | read a declared channel, judge by wording, skip a structured source |
| `tests/unit/test_forge_recording_acceptance_corpus.py` rows `v27_*` (0.27; 0.28: `v27_stdin_file_cited_in_a_sentence_is_ordinary` becomes `v28_undeclared_stdin_sentence_is_a_structured_source`; seven NG4-16 residual rows — body, JSON string in a body, review path, reserved owner in a body, code block, GitLab body, raw stdout —; a reserved owner, an error line, nested method args and a file with a fragment read in a structured source; directory paths under `repos/` ordinary in a body, a review path, a code block, a monorepo layout and a stdin sentence) | 0.27 (a), (b) | read prose, read a file cited in a sentence, skip a structured source |
| `tests/unit/test_forge_recording_acceptance_corpus.py` rows `v26_*` (0.26; 0.27: four renamed `v27_*_NG4_16`: a GitHub API path with a REST route word in a body, kwargs, stdin, a JSON string, a review comment's `path`, with a reserved owner; a file after the repository and a bare path, ordinary; a second exchange's argv; a URL-field namespace before an argv repository) | 0.26 (a), (b), (c) | read no text API path, read a bare one, read one exchange, read argv first |
| `tests/unit/test_forge_recording_acceptance_corpus.py` rows `v25_*` (0.25: six ordinary texts led by `repos/` or `projects/` in a body or a review comment's `path`, reserved words and a plain directory; an argv `repos/settings/<r>` still read; a GitLab `web_url` under a nested group learning no joined value) | 0.25 (a) and (b) | read every string as an argv path, join for every hard source |
| `tests/unit/test_forge_recording_acceptance_corpus.py` rows `v24_*` (0.24: three nested remotes under a reserved root, two argv API paths, two forwarded public scopes in code, the company scope NG4-15, a scope equal to the remote namespace; any of the three 0.24 rules reverted turns one red) and rows `v23_*` and `v22_gitlab_s_redirect_identity_after_a_reserved_word_NG4_14` (0.23; 0.24: the five `v23_gitlab_npm_scope_*` rows corrected to NG4-15) | B1 (five rows: GitHub `assets` replaced and `jwt` refused, the instance's `snippets`, an https `help` remote, `uploads` replaced in a route too), B2 (eight rows: the three endpoints, the file name, the encoded path, a third-party instance, the unscoped package; NG4-15 and the Nexus control), the casefolded root (one row), NG4-14 live (one row) | any of the three rules reverted |
| `tests/unit/test_forge_recording_acceptance_corpus.py` rows `v22_*` (0.22) | the four classes as the 0.21 verifier's cells met them: the Web IDE (three rows, the instance and a third party), the reserved words of four forges (four rows), Bitbucket Server's two routes and GitHub's team slug (three rows), the npm tarball on yarnpkg, npmmirror, a private registry and a Nexus in a yarn.lock diff (four rows) | any of the four rules reverted |
| `::test_nothing_is_read_after_a_placeholder_class_host` (0.20) | C3, NG4-12: IPv4, IPv6 and name placeholders, public hosts: no segment learnt | read after an allowed literal (R12) |
| `::test_an_ipv6_url_in_text_is_rewritten_by_the_convenience_layer_and_is_a_fixed_point` (0.20, 0.21) | C8: a bracketed host is a text URL, rewritten without its port, kept byte-identical but for case, a fixed point; 0.21: `_kept_host` compares the placeholder casefolded | drop the bracketed host (R16), compare case-sensitively (R13) |
| `::test_language_keywords_after_an_at_sign_are_no_mention` (0.20) | C10, C11: every word of the four sets, upper-cased too, the userinfo block | read a tag as a handle (R14), drop `:${}` (R13) |
| `::test_a_short_mention_only_handle_is_replaced_in_its_at_form_and_never_refuses` (0.20) | C12: no `NeedleError`, no value, the `@` form replaced | add the short handle to K (R15) |
| `tests/unit/test_forge_closure.py::test_a_quad_after_a_single_slash_is_a_version_and_a_comparator_allows_spaces` (0.20) | C4, C59: a path-segment quad, a comparator with spaces or tabs, a followed quad after one | drop the single-slash rule (C67), the spaces (C59) |
| `::test_the_unspecified_and_broadcast_addresses_are_placeholder_class` (0.20) | C5 | drop `0.0.0.0` (C66), `::` (C66b) |
| `::test_the_oracle_never_raises_on_a_mapped_tail_or_a_long_digit_run` (0.20) | C6: `[::ffff:01.2.3.4]`, `U_` + 5 000 digits, the twenty-digit bound | build from the raw text (C64), drop the bound (C63) |
| `::test_json_and_hex_string_escapes_are_a_decoded_layer` (0.20) | C7: `\/` and `\xHH` peeled, an ordinary regex untouched | drop `\/` (C61) or `\x` (C62) |
| `::test_a_mailbox_on_a_placeholder_address_names_no_one` (0.20) | C9: loopback, unspecified, broadcast, RFC 5737 literal; private, public, credential, `forge.example.com` not | drop the clause (C65), accept a credential (C65b) |
| `::test_the_readers_are_linear_in_the_text` (0.20) | C15: 5 000 URLs and a 1 MB diff under a bound, every lead shape unchanged | require no letter in a scheme (C69) |
| `::test_host_rule_tld_lists_reverse_dns_heads_and_extension_codes_by_enumeration` (0.17, 0.20) | the 23 extension codes and ten `<file>:<line>` diagnostics | drop `cu` (C70), `sv` (C70b) |
| `::test_public_allowlist_and_placeholder_class_by_enumeration` (0.17, 0.20) | the 26 suffixes, `api.nuget.org` | drop `nuget.org` (C71) |
| `tests/unit/test_forge_recorder_sweep.py::test_the_sweep_states_every_rule_and_names_at_least_two_thousand_cells` and `::test_a_deterministic_sample_of_the_sweep_meets_the_owners_goal` (0.20, 0.21) | AC8: 6 174 cells in twelve rules (0.21: `routes`, `logins`, `nesting`, the `BOUNDED` category and the route words held unreplaced; 0.22: every forge's reserved words, the Web IDE, the owner-root slots and the npm tarballs in `routes`, 1 183 cells) with an expectation from the rule set, a deterministic fifth (≥ 500) through `record()` live and offline: no crash, no leak outside the residuals, no change of ordinary text outside the documented rewrites | any rule of D7, D8, D1b or D2 widened or narrowed |
| `tests/unit/test_forge_recording_acceptance_corpus.py::test_the_planted_and_ordinary_split_is_computed_from_the_rows` (0.19, 0.20) | T01: the five counts and every residual planted | drop the residual term of `_is_planted` |
| `::test_known_values_read_a_gitlab_commit_objects_author_and_committer_names` (0.19) | D5: `author_name` and `committer_name` beside their mailboxes | read `name` only |
| `::test_path_identity_names_the_namespace_and_the_repository_by_enumeration` (0.18) | the GHE `…/api/v3/repos/<o>/<r>/` `url` key raises no `NeedleError`, versions skipped by rule | drop the version skip |
| `::test_offline_roles_walk_the_keyword_arguments_and_refuse_positional_logins` (0.18) | D4: logins under keyword arguments walked, a positional login refused | walk `args` |
| `::test_offline_redaction_rewrites_urls_in_arguments_keyword_arguments_and_results` (0.18) | D4: the URL rewrite of arguments, keyword arguments and results | skip `kwargs` or `result` |
| `tests/unit/test_logging_cache_disabled_for_tests.py::test_6_capture_logs_sees_a_debug_event_after_the_level_filter` (0.18) | D4 test order: the wrapper class is reset before each test | remove the reset |
| the unchanged 0.6–0.16 tests of `test_forge_recording_redaction.py` not retired in **What 0.17 removes or demotes** | D1, D1c, D2, D5, D6, the deny-list route and opacity, the conformance replay | as at 0.16 |
| `tests/unit/test_forge_recording_regression_corpus.py` (0.14–0.16) | folded into the acceptance corpus and deleted (0.17): its rows are the `reg_*` rows | as at 0.16 |
| `tests/unit/test_forge_conformance.py` (existing) | D4, C2 | as at 0.6 |

## Success conditions

- C1 (oracle: pytest, CI): `pytest -q tests/unit/test_forge_recording_redaction.py tests/unit/test_forge_recording_acceptance_corpus.py tests/unit/test_forge_closure.py tests/unit/test_forge_conformance.py tests/unit/test_review_gate_forge_parity.py tests/unit/test_forge_recorder_sweep.py` → green, the deny-list test by the file route; the acceptance module without its `xfail` mark (0.20: the sweep module joins the condition).
- C2 (oracle: the script and git): `scripts/redact_forge_fixtures.py --check` → `forge fixtures: 0 would change`, exit 0; the rebuild from `7fd31af`'s fixtures (the two hand edits of 0.10 applied first) is byte-identical to the branch's, a second run changes 0 files; `git grep -o -E 'https?://([a-z0-9.-]+\.)?(github|gitlab)\.com' -- tests/fixtures/forge` → empty but `docs.*`.
- C3 (oracle: the script, by the implementer, output written beside AC5): the six read methods of 0.11 against repoachhq/repoach PR #316 (`--trim 5`, `--cwd` the checkout, the deny-list by the file route), each exit 0, 0 real-value hits in the six files, measured with the maintainer's own deny-list and reported as counts.

## Acceptance Criteria

- [x] AC1: the acceptance corpus passes, live and offline, with its `xfail` mark removed (`pytest -q tests/unit/test_forge_recording_acceptance_corpus.py` → `2 passed`); no row edited to match the code but the six the owner authorised (amendment 0.18 (e)), the seventh that follows from D1 (`code_three_label_private_host_schemeless_no_path`) and the three the IPv4 decision and `apache.org` move (amendment 0.19 (d)); the counts written here, computed from the file by `tmp/fix018/count_rows.py` and pinned by a test. Counts (implementer, 2026-11-22, 0.19): 752 rows = 523 planted (a marker, a refusal or a residual) + 229 ordinary = 684 written + 68 refused, 10 of the planted being documented residuals; by source 260 `reg_*` rows of the folded regression corpus, 139 round-10 probes, 170 generated along the 0.17 axes, 155 `v17_*` rows of the 0.17 verifier's probes (84 planted, 71 ordinary) and 28 `v18_*` rows (18 planted, 10 ordinary). Measured at `fb79648`: every row passes live and offline. **0.20** (Fable, 2026-11-23, `tmp/fable020/count_rows.py`, pinned by the split test): 800 rows = 545 planted + 255 ordinary = 729 written + 71 refused, 16 residuals; 48 rows `v20_` (22 planted, 26 ordinary, 3 residuals — this line said 4); four rows corrected under the audit's proof, each cited in its commit: `v17_private_ipv4_path_followed_NG4_2` and `v18_followed_private_ipv4_port_path_NG4_2` (C1), `v17_ipv6_doc_url_path_NG4_12` (C3), `v17_prefixed_label_known_host` (C2); one renamed, `v18_v13_ipv6_unled_ula_path_NG4_10`. **0.21** (Fable, 2026-11-23, `tmp/fable020/count_rows.py`, pinned by the split test): 815 rows = 558 planted + 257 ordinary = 742 written + 73 refused, 18 residuals; 15 rows `v21_` (13 planted, 2 ordinary, 2 residuals); no row corrected. **0.22** (Fable, 2026-11-23, pinned by the split test): 829 rows = 568 planted + 261 ordinary = 756 written + 73 refused, 18 residuals; 14 rows `v22_` (10 planted, 4 ordinary, 0 residuals); no row corrected (`reg_short_third_party_url_x_com` is why `user` is not a reserved word). **0.23** (Fable, 2026-11-23, pinned by the split test): 844 rows = 583 planted + 261 ordinary = 770 written + 74 refused, 18 residuals; 14 rows `v23_` (14 planted, 1 of them refused, 0 ordinary) and one more `v22_` row (15 `v22_`: 11 planted, 4 ordinary); no row corrected. **0.24** (2026-11-23, pinned by the split test): 853 rows = 592 planted + 261 ordinary = 779 written + 74 refused, 18 residuals; 9 rows `v24_` (9 planted, 0 refused, 0 ordinary); five `v23_gitlab_npm_scope_*` rows corrected and renamed under 0.24 (c), cited in the test commit. **0.25** (2026-11-23, pinned by the split test): 861 rows = 594 planted + 267 ordinary = 787 written + 74 refused, 18 residuals; 8 rows `v25_` (2 planted, 0 refused, 6 ordinary); no row corrected. **0.26** (2026-11-24, pinned by the split test): 871 rows = 602 planted + 269 ordinary = 797 written + 74 refused, 18 residuals; 10 rows `v26_` (8 planted, 0 refused, 2 ordinary); no row corrected; one row declares `url_field_class`.
- [x] AC2: the oracle independence test passes (`test_the_oracle_shares_no_code_object_with_the_redactor`).
- [x] AC3: a mutation run on `src/repoach/forge/closure.py` — every line of every reader and every set mutated by the operator classes of `tmp/impl017/mutate.py` (comparison, boolean, negation removal, constants, literal elements, method swaps, dropped calls and `continue`), run in detached worktrees with a verified green baseline, strict kill = rc 1 plus a `FAILED` line — leaves **0 non-equivalent survivors**; each equivalent survivor named with its proof; log written under `tmp/impl017/mutate.log` (0.17) and `tmp/fix017/mutate.log` (0.18) and `tmp/fix018/mutate.log` (0.19) and summarised here. Measured at 0.18 (`tmp/fix017/mutate.py`, seven detached worktrees): `closure.py` 1 324 sites, 1 277 killed, 23 mutants that cannot import (a constant or a `+` mutated in `IPV4_RESERVED`, `_RFC5737`, `_IPV6_DOCUMENTATION` or `LOCAL_CHARS` raises at import), 16 equivalent survivors (14 country codes of `_ISO_COUNTRY_CODES` and `ac` of `_RESERVED_COUNTRY_CODES` that are also in `EXTENSION_TLDS` and are subtracted anyway, and `run[:40]` 40 to 41, which the `len % 4 == 1` alignment trims back to 40; each proved by comparing every public constant and the outputs of every reader on 4 000 and more inputs, `tmp/fix017/equiv.py`), 8 identical, 0 non-equivalent survivors. Two mutants of that run were real and are gone: `set(g) <= _HEX` of the IPv6 reader (always true for a group of four characters, an equivalent mutation kept in the code, rewritten `all(c in _HEX for c in g)`) and `count(":") > 3` (a global literal with exactly three colons, now pinned). The functions changed in `recording.py` (`known_values`, `path_identity`, `mentions`, `CSS_AT_RULES`, `_API_RESOURCES`): 430 sites at the first run, 337 killed, 93 survivors (90 in the at-rule set, which the test iterated instead of enumerating, `mentions`' code-span blanking and two `.casefold()` of `path_identity`), all killed after the tests and the exact-case rule of `afd7ef0` and `be0a518`; second run 267 sites, 266 killed, one survivor (`> 3` of the versioned prefix), killed by `03085ef`. `redact_forge_fixtures.py` (`redact_fixture`, `legacy_roles`, `_unclassified_logins`, final code) 114 sites, 112 killed, 2 non-terminating (the loop condition of the next free class word: the suite never ends), 0 survivors; two equivalent mutations of the first run (`args` and `kwargs` in the list read by `raw_logins`) are gone, the code reading the exchanges only (`232a218`). Logged in `tmp/fix017/mutate.log`. Measured at 0.19 (`tmp/fix018/mutate.py`, seven detached worktrees, the functions changed in this pass, log `tmp/fix018/mutate.log`): `closure.py` (`hosts`, `is_address`, `is_public_address`, `public_addresses`, `judge`, `_ipv6`, `_bracketed`, `_BRACKETED`, `PUBLIC_SUFFIXES`) 179 sites, 176 killed, 2 identical, 1 survivor, equivalent (the `":" in body` test of the dotted IPv4 tail of `_ipv6`: a body without a colon reads as no address with or without it, so the test is removed, `bfe8eaa`; second run of `_ipv6`, 36 sites, 35 killed, 1 identical, 0 survivors); `recording.py` (`known_values`, `_MAILBOX_KEYS`, `_NAME_KEYS`) 178 sites, 177 killed, 1 survivor that was real (the key test of the name-beside-mailbox rule: a lone `author_email` learnt a mailbox), killed by an assertion (`45de36b`, re-run 4 of 4 killed). Hand mutants: the verifier's C25 (`judge` decoding before blanking the literals) is killed by `test_the_judge_blanks_the_literals_before_it_decodes`; the 0.19 forms of C06 to C08 (hosts, public addresses and mailboxes on the raw form only) and fourteen mutants of the new IPv4, IPv6 and allowlist code are killed, C07 after an assertion that `judge` reads an IPv6 literal in its decoded forms (`fb79648`); C10 to C12 mutated `_version_shaped`, which is deleted. The four mutants that drop `author_name`, `committer_name`, `author_email` or `committer_email` from the corpus harness survived while the rows cited those keys at the top of the item, where their values are raw strings of the row itself; four nested rows (`a0917ee`) kill all four. 0 non-equivalent survivors; no function of `redact_forge_fixtures.py` or `record_forge_fixture.py` changed. **Measured at 0.20** (`tmp/fable020/mutate.py`, five detached worktrees, log `tmp/fable020/mutate.log`): `closure.py`, every rule, 1 408 sites = 1 341 killed + 9 identical + 30 equivalent (21 ISO codes, `ac` and `pro`, each also in `EXTENSION_TLDS` and subtracted anyway; a lower bound at offset 0 that no scheme, `@` or comparator can precede; a `>` to `>=` on the two separator loops, which stop one step later on an all-separator prefix with the same outcome; `run[:40]` to `run[:41]`, trimmed back by the alignment; each proven by comparing every reader's output on 5 477 inputs, `tmp/fable020/equiv.py`) + 28 that cannot import (an empty address in `_UNSPECIFIED_BROADCAST`, `IPV4_RESERVED`, `_RFC5737` or `_IPV6_DOCUMENTATION`, a `-` for a `|` or `+` on a frozenset or a string; each shown to raise at import by the same script) + **0 non-equivalent survivors**: seven operator mutants of the first run were real (the digits and `+.-` of a scheme, a comparator, a single `/` or a `//` at offset 0, the `literal` default) and are killed by the assertions of `test_the_readers_are_linear_in_the_text`. `recording.py` (`known_values`, `_api_route`, `_follow_segments`, `_path_after`, `mentions`, `_URL_IN_TEXT_RE` and its parts, `_MENTION_BEFORE_BLOCK_CLASS`, `LANGUAGE_AT_WORDS`, `redact_urls_in_text`): 337 sites = 328 killed + 3 equivalent (two casefolds a callee repeats, a log string) + 6 that cannot import (an unbalanced regex, a `-` on a string) + 0 non-equivalent survivors: seven were real (the two early returns of the text-URL rewrite, the case of a noreply host and local, a host that only ends in `forge.example.com`, two leading underscores) and are killed by `test_an_ipv6_url_in_text_is_rewritten_by_the_convenience_layer_and_is_a_fixed_point`. Hand mutants (`tmp/fable020/hand_mutants.py`, one per rule of 0.20 plus the verifier's C59, T01 and R10): 25 of 25 killed. The 0.20 verifier's own 72 hand mutants (`tmp/verify020/mutants_v2.py`) left three alive that the operator classes above do not generate (a constant 45 → 39, a regex quantifier `{1,5}` → `{1,4}`, a dropped `casefold()` in `_kept_host`, which was not in the function list): M21, R01, R13, pinned at 0.21. **Measured at 0.21** (`2be2cea`, `tmp/fable021/mutate.py`, five detached worktrees, log `tmp/fable021/mutate.log`): `closure.py`, every rule, 1 408 sites = 1 341 killed + 9 identical + 30 equivalent + 28 that cannot import + **0 non-equivalent survivors**, the 58 survivors the same set as 0.20's and each proven again by `tmp/fable021/equiv021.py` over 7 538 inputs (`tmp/fable021/equiv.log`); `recording.py` (`path_identity`, `_api_route`, `decode_json_values`, `_API_RESOURCES`, `_API_ROOTS`): 206 sites = 204 killed + 1 identical + 1 survivor that was real (the trailing slash of a `/-/<route>/` path with nothing before the separator), pinned by `78a1958` and killed on the rerun; the 0.20 verifier's 72 hand mutants (`tmp/fable021/mutants_v2.py`): 69 killed as written, M21, R01 and R13 among them, and the three whose text no longer matched (R14, V17, R17) re-expressed on the 0.21 lines and killed (`tmp/fable021/hand_rerun.log`), 72 of 72; the 0.20 hand mutants: 24 of 24 that applied killed, R11b re-expressed and killed; nine hand mutants of the 0.21 rules (the dash separator dropped from `_api_route`, a leading dash counted, the one-segment branch of `path_identity` removed, the owner root reading one segment, the `RecursionError` catch and the two guards dropped) all killed. **Measured at 0.22** (`d6b6a13`, then the rerun at `cff1bc8`; `tmp/fable022/mutate.py`, five detached worktrees, log `tmp/fable022/mutate.log`): `recording.py`, the changed functions (`path_identity`, `_api_route`, `_follow_segments`, `_ide_route`, `_project_root`, `_package_tarball`, `_RESERVED_ROUTE_WORDS`, `_IDENTITY_SLOTS`, `_IDE_ROUTES`, `_TARBALL_VERSION_RE`): 399 sites = 390 killed + 9 survivors at the first run, each a shape no assertion covered (a Web IDE project of one segment followed by a route, the stop index, the route and the trailing slash of that branch; the two casefolds of `_project_root`, whose fall-through to the generic reading replaced the same two segments; a one-character tarball version; a tarball separator that is not a dash; the `return False` of the tarball check asserted as falsy) and killed by `cff1bc8` on the rerun of those nine sites (9 of 9); `closure.py` (`PUBLIC_SUFFIXES`, `is_public_host`): 62 sites, 62 killed; 48 hand mutants of the 0.22 rules (`tmp/fable022/mutants022.py`: the reserved table emptied, a word dropped, the case, the check moved after the dash branch; the IDE stop index, the stop word, the lone branch, the three tests of `_ide_route`, the IDE word dropped from `_api_route`; the slot table emptied, a slot dropped, a team written as a project, the case of the root and of the slot word, the slot's segment count, the raw values; the project root dropped from the owner branch and from `_follow_segments`, its segment count, its `repos` word; the tarball check dropped, each of its three tests, the version regex loosened and made case-sensitive, the two guards; the two allowlist suffixes; the eight 0.21 verifier mutants whose text changed, re-expressed): 47 killed + H33 (`any package name`) alive at the first run, since the only non-tarball negative had a name of another length, killed on the rerun by `lodasx-4.17.21.tgz`; the 0.21 verifier's 29 mutants (`tmp/verify021/mutants.py`): 21 killed as written, 8 whose text changed re-expressed above and killed; the 0.20 verifier's 72 (`tmp/verify020/mutants_v2.py`): 68 killed as written, 4 whose text changed (R04, R14, V17, R17) re-expressed on the 0.22 lines and killed (`tmp/fable022/hand_rerun.log`). **0 non-equivalent survivors.** **Measured at 0.23** (`2de9f27`; `tmp/fable023/mutate.py`, five detached worktrees, log `tmp/fable023/mutate.log`): `recording.py`, the changed functions (`path_identity`, `_repository_path`, `_npm_registry`, `_NPM_SCOPE_RE`, `known_values`): 472 sites = 469 killed + 2 identical + 1 equivalent (`head[2:3]` → `head[2:4]`, the same truth value whenever `head[2]` exists); at `66d4127` a second survivor was real (the casefold of the `projects`/`groups` check before `packages/npm`) and is killed by `2de9f27` (an encoded `Projects` prefix in `test_a_gitlab_npm_scope_is_a_root_group`); 30 hand mutants of the 0.23 rules (`tmp/fable023/mutants023.py`): 29 killed + M15 equivalent (the registry search started at index 0: `core[0]` is `api` and `core[1]` matches `^v[0-9]+$`, so neither is `packages`); the 0.22 verifier's 41 (`tmp/verify022/mutants.py`, run from `tmp/fable023/mutants_v022.py`, which differs only by its output directory and by V23 re-expressed on the casefolded line): 40 killed + V38 equivalent (a version followed by `.tgz` matches the version regex if and only if the version does). **0 non-equivalent survivors.** **Measured at 0.24** (`064c4ff`, then `85a47ce`; `tmp/fix024/mutate.py`, five detached worktrees, log `tmp/fix024/mutate.log`): `recording.py`, the changed functions (`path_identity`, `_npm_registry`, `known_values`): 458 sites = 454 killed + 2 identical + 2 equivalent survivors (`len(identity.namespace) + 1` to `+ 2` in the joined-namespace loop: the extra slice is the whole namespace again, a duplicate `_unique` drops, proven on 2 718 remotes and argv paths, `tmp/fix024/equiv341.py`; `head[2:3]` to `head[2:4]`, the 0.23 equivalent, proven on 13 860 paths, `tmp/fix024/equiv_pi.py`); 14 hand mutants of the 0.24 rules (`tmp/fix024/mutants024.py`: the joined values dropped, started at three, only the whole path, one short, for every source, soft, written as a project, joined by `%2F`; the argv path by the route grammar and as a soft source; the npm scope written `@org` and learnt again, the prefix not read, the branch removed): 14 killed; the 0.23 verifier's 25 (`tmp/verify023/mutants023.py`, run from `tmp/fix024/mutants023.py`, which differs only by its output directory): 16 killed, 7 whose line 0.24 deletes (`_NPM_SCOPE_RE` and the scope rewrite: N07, N08, N10, N11, N12, N22, and N09, re-expressed on the 0.24 line and killed), N13 killed (pinned by 0.24 (d)), N01 equivalent (`len(core) < 3`: a three-segment `api/vN/<x>` has no `packages/npm` pair, proven on 13 860 paths), N17 alive at the first run — real: `api/vN/groups/<g>/packages/npm/…` without its dash read the package as a namespace by the generic reading — killed on the rerun by `85a47ce` (`tmp/fix024/mutants_rerun.log`, 3 of 3); the 0.22 verifier's 41 (`tmp/verify022/mutants.py`, run from `tmp/fix024/mutants_v022.py` with V23 re-expressed on the casefolded line as at 0.23): 40 killed + V38 equivalent (as at 0.23). **0 non-equivalent survivors.** **Measured at 0.25** (`64a2162`, `tmp/fix025/run_mut.sh`, log `tmp/fix025/mutate.log`): `_argv_strings`, the new function, every operator site (`tmp/fix025/mutate.py`): 11 sites, 11 killed; 16 hand mutants of the changed lines (`tmp/fix025/hand025.py`: every string read as an argv path, the 0.23 text reading kept or read soft, the argv loop dropped, argv by the route grammar and soft, the exchange test without `stdout`, with `program`, negated, `args` not a list, non-strings kept, no dict or list walked, strings decoded, one argv string, the first dropped): 16 killed; the 0.24 verifier's 26 (`tmp/verify024/hand024.py`): 24 killed, V09 among them, + V02 and V26 equivalent (proven on 2 718 remotes and argv paths with mixed case, the joined value of one segment is the segment `_unique` drops and every value is casefolded when entered, `tmp/fix025/equiv_v.py`); the 0.23 verifier's (`tmp/verify023/mutants023.py`): 17 killed + 7 not applicable (the deleted `_NPM_SCOPE_RE` lines) + N01 equivalent (as at 0.24). **0 non-equivalent survivors.** **Measured at 0.26** (`tmp/fix026/mut26.py`, log `tmp/fix026/mut26.log`, on the final code): 106 mutants: the changed lines (36 of the text API reader, its pattern, its allowlist and `_names_a_value`; 21 of the table, its sources and its readers), the 0.25 verifier's set re-expressed on the 0.26 code (B01 to B23, V09) and the 0.24 verifier's 26 (V01 to V26, V13 to V15 re-expressed as table rows): 97 killed, B09 and B19 among them, + 9 equivalent: B02, B06, B21 (an exchange's `args` is `Exchange.args`, a list of strings), B08, B14, B20 (the only exchange objects of a recording are its calls, `raw[0]`, in `record()` and in `redact_fixture()`), B15 (a repeated argv string yields the values of its first occurrence), V02 and V26 (the 0.25 proof, `tmp/fix025/equiv_v.py`); each one's known values are identical to the original's on 3 000 reachable recordings (`tmp/fix026/equiv26.py`, `tmp/fix026/equiv_pop.py`, log `tmp/fix026/equiv26.log`), a population on which B09, B13, B16, B17, B19 and N27 all differ. A first run on the code before its last edit left 8 more survivors, each closed: N05 (the API version widened to `api/v<N>/`, Gitea's `api/v1/`), N09 and N12 (two unreachable branches removed), N23, N27, B13, B16, B17 (tests added). **0 non-equivalent survivors.**
- [x] AC4: C1 green; C2 with the rebuild byte-identical; C3 (live PR #316, `--trim 5`) green; figures written here with their commands. Measured at `fb79648`: C2 `scripts/redact_forge_fixtures.py --check` → `forge fixtures: 0 would change`, the rebuild from `origin/develop` with the two hand edits is byte-identical (0 diff lines, 96 files, a second run changes 0 files), `git grep` for a forge host in `tests/fixtures/forge` → 0; C3 the six read methods exit 0, 0 real-value hits in the six files and their stderr (37 needles); C1 (`pytest -q` of the five files of the condition) → 271 passed (the 0.18 line said 261; the verifier measured 263 at `8073555`). **Measured at 0.20** (`fca0300` and the two docs commits after it): C1 (`pytest -q` of the six files of the condition, the sweep module included) → 282 passed; C2 `scripts/redact_forge_fixtures.py --check` → `forge fixtures: 0 would change`, the rebuild from `origin/develop` with the two hand edits byte-identical (0 diff lines, 96 files, a second run changes 0 files; `tmp/fable020/c2_rebuild.sh`); C3 the six read methods against PR #316 with `--trim 5` exit 0, 0 real-value hits in the six files and their stderr (37 needles, 31 deny-list entries; `tmp/fable020/run_c3.sh`); the three verifier probe sets of 0.17, 0.18 and 0.19 rerun: 0 crashes, 0 over-refusals, every leak inside NG4-1/2/3/4/7/8/9/10/11/12, 1 documented forge-host rewrite of ordinary text (`go_mod_file`). **Measured at 0.21** (`2be2cea`): C1 (the six files) → 286 passed; C2 `--check` → `forge fixtures: 0 would change`, the rebuild from `origin/develop` byte-identical (0 diff lines, 96 files, a second run changes 0 files; `tmp/fable021/c2_rebuild.sh`); C3 the six read methods against PR #316 with `--trim 5` exit 0, 0 real-value hits (37 needles, 31 deny-list entries; `tmp/fable021/run_c3.sh`); the three probe sets identical to their 0.20 run line for line (`tmp/fable021/probes01*.log`); the verifier's `repro_blocking.py`: the eight L1 rows written with the namespace replaced live and offline, a fixed point, the L2 rows written as NG4-13 says. **Measured at 0.22** (`d6b6a13`, C1 rerun at `cff1bc8`): C1 (the six files) → 291 passed (`tmp/fable022/c1.log`); C2 `--check` → `forge fixtures: 0 would change`, the rebuild from `origin/develop` byte-identical (0 diff lines, 96 files, a second run changes 0 files; `tmp/fable022/c2_rebuild.sh`), 0 forge hosts in the fixtures; C3 the six read methods against PR #316 with `--trim 5` exit 0, 0 real-value hits (37 needles, 31 deny-list entries; `tmp/fable022/run_c3.sh`); the 017 and 019 probe sets identical to their 0.21 run line for line, the 018 set identical but for `p02_unknown_host_dot_segment` (amendment 0.22 (g), NG4-14); the verifier's `repro_blocking.py` and `hostile.py` unchanged (L1 closed, L2 as NG4-13 says; 181 cells, 0 crashes). **Measured at 0.23** (`2de9f27`, the last test commit): C1 (the six files) → 294 passed (`tmp/fable023/c1.log`); C2 `--check` → `forge fixtures: 0 would change`, the rebuild from `origin/develop` byte-identical (0 diff lines, 96 files, a second run changes 0 files; `tmp/fable023/c2_rebuild.sh`), 0 forge hosts in the fixtures; C3 the six read methods against PR #316 with `--trim 5` exit 0, 0 real-value hits (37 needles, 31 deny-list entries; `tmp/fable023/run_c3.sh`). **Measured at 0.24** (`19878b3`, code as at `064c4ff`; C1 rerun at `85a47ce`): C1 (the six files) → 295 passed (`tmp/fix024/c1.log`); C2 `--check` → `forge fixtures: 0 would change`, the rebuild from `origin/develop` byte-identical (0 diff lines, 96 files, a second run changes 0 files; `tmp/fix024/c2_rebuild.sh`), 0 forge hosts in the fixtures; C3 the six read methods against PR #316 with `--trim 5` exit 0, 0 real-value hits (37 needles, 31 deny-list entries; `tmp/fix024/run_c3.sh`); the 017, 018 and 019 probe sets, `repro_blocking.py` and the 0.21 verifier's `cells.py` identical to their 0.23 run line for line; `hostile.py` 181 cells, 0 crashes, 32 named refusals. **Measured at 0.25** (`192682d`): C1 (the six files) → 297 passed (`tmp/fix025/c1.log`); C2 `--check` → `forge fixtures: 0 would change`, the rebuild from `origin/develop` byte-identical (0 diff lines, 96 files, a second run changes 0 files), 0 forge hosts in the fixtures; C3 the six read methods against PR #316 with `--trim 5` exit 0, 0 real-value hits (37 needles, 31 deny-list entries; `tmp/fix025/run_c3.sh`); the 017, 018 and 019 probe sets identical to their 0.24 run line for line; `repro_blocking.py`, the 0.20 verifier's `hostile.py` (its 20 000-URL cases left out) and the 0.21 verifier's `hostile.py` identical to their run at `20de0b5` (`tmp/fix025/allcells.sh`). **Measured at 0.26** (`22ad005`): C1 (the six files) → 301 passed; C2 `--check` → `forge fixtures: 0 would change`, the rebuild from `origin/develop` byte-identical (0 diff lines, 96 files, a second run changes 0 files), 0 forge hosts in the fixtures (`tmp/fix026/gates_light.log`); C3 the six read methods against PR #316 with `--trim 5` exit 0, 0 real-value hits (37 needles, 31 deny-list entries; `tmp/fix026/run_c3.sh`, `tmp/fix026/c3.log`); `repro_blocking.py`, the 0.20 verifier's `hostile.py` (its 20 000-URL cases left out), the 0.21 verifier's `hostile.py` (its load-dependent `slow` lines masked), `sweep_v2.py` and `parity_probe.py` identical to their runs at `fb2bf94` and `20de0b5` (`tmp/fix026/bank.sh`, `tmp/fix026/bankcmp.py`, `tmp/fix026/bankcmp.log`). **Measured at 0.27** (`346cc8c`): C1 (the six files) → 306 passed (`tmp/fix027/c1.log`); C2 `--check` → `forge fixtures: 0 would change`, the rebuild from `origin/develop` byte-identical (0 diff lines, 96 files, a second run changes 0 files), 0 forge hosts in the fixtures (`tmp/fix027/gates_light.log`); C3 the six read methods against PR #316 with `--trim 5` exit 0, 0 real-value hits (37 needles, 31 deny-list entries; `tmp/fix027/run_c3.sh`, `tmp/fix027/c3.log`); the cumulative bank (`tmp/fix027/bank.sh`: the 0.20 to 0.26 verifiers' cells, the six probe sets the 0.26 bank omitted and `cells26.py`, `cells26b.py`) against `0d604fe`, `fb2bf94` and `20de0b5` (`tmp/fix027/bankcmp27.py`, `tmp/fix027/bankcmp27.log`): every plain log identical but the corpus counts and `repro_leak.py`'s two prose lines (NG4-16); the 0.21 to 0.24 verifiers' cells identical verdicts to `0d604fe`.
- [x] AC5: the full unit suite (`tmp/r6/clean_env.sh .venv/bin/python -m pytest -q tests/unit -n 4`), `ruff check`, `ruff format --check` (`python -P -m ruff`, the pinned version) and `repoach arch check --base origin/develop` → green, `edge-honesty: ok`, the cycle and conflict lines identical to `origin/develop`'s. Measured at `fb79648`: `tmp/r6/clean_env.sh` full `tests/unit -n 4` → 4 728 passed, 64 skipped, 0 failed (4 792 collected); `ruff check` and `ruff format --check` (`python -P -m ruff`) clean; `repoach arch check --base origin/develop` rc 0, `edge-honesty: ok`; `repoach arch graph --check` 27 cycles and 5 conflicts, the same lines as `origin/develop` (at 0.17 the suite gave 4 703 passed and 2 failed, not the "4 704 passed, 1 failed" this line first said: `test_known_values_ids_ordering_and_json_in_strings` depended on the run order, D4). **Measured at 0.20** (`fca0300`): `tmp/r6/clean_env.sh` full `tests/unit -n 4` → 4 741 passed, 64 skipped, 0 failed in 12 min 39 s (`tmp/fable020/unit.log`); `ruff check` and `ruff format --check` (`python -P -m ruff`) clean, 707 files; `repoach arch check --base origin/develop` rc 0, `edge-honesty: ok`; `repoach arch graph --check` 27 cycles and 5 conflicts, 0 lines of difference against `origin/develop` `8ee2c38` measured on an archive of it (`tmp/fable020/arch.log`). **Measured at 0.21** (`2be2cea`): `tmp/r6/clean_env.sh` full `tests/unit -n 4` → 4743 passed, 64 skipped in 0:11:49 (`tmp/fable021/unit.log`); `ruff check` and `ruff format --check` clean, 707 files; `repoach arch check --base origin/develop` rc 0, `edge-honesty: ok`; `repoach arch graph --check` 27 cycles and 5 conflicts, 0 lines of difference against `origin/develop` `8ee2c38` (`tmp/fable021/arch.log`). **Measured at 0.22** (`cff1bc8`, the last test commit): `tmp/r6/clean_env.sh` full `tests/unit -n 4` → 4748 passed, 64 skipped in 0:12:40 (`tmp/fable022/unit.log`; 0:12:28 at `d6b6a13`); `ruff check` and `ruff format --check` clean, 707 files; `repoach arch check --base origin/develop` rc 0, `edge-honesty: ok`; `repoach arch graph --check` the same cycle and conflict lines as `origin/develop` `8ee2c38`, measured on an archive of it with `clean_env.sh` called by its absolute path (`tmp/fable022/arch.log`; both exit 2 on the same cycle). **Measured at 0.23** (`66d4127`, the last test commit; the docs commits on top of it change no code or test): `tmp/r6/clean_env.sh` full `tests/unit -n 4` → 4751 passed, 64 skipped in 0:12:57 under the mutation run (`tmp/fable023/unit.log`); `ruff check` and `ruff format --check` clean, 707 files; `repoach arch check --base origin/develop` rc 0, `edge-honesty: ok`; `repoach arch graph --check` the same cycle and conflict lines as `origin/develop` `8ee2c38`, measured on an archive of it with `clean_env.sh` by its absolute path (`tmp/fable023/arch.log`; both exit 2 on the same cycle, 2 diff lines as at 0.22). **Measured at 0.24**: `ruff check` and `ruff format --check` (`python -P -m ruff`) clean, 707 files, at `85a47ce`; `repoach arch check --base origin/develop` rc 0, `edge-honesty: ok`; `repoach arch graph --check` the same cycle and conflict lines as `origin/develop` `8ee2c38`, measured on an archive of it with `clean_env.sh` by its absolute path (`tmp/fix024/arch.log`; both exit 2 on the same cycle, 2 diff lines as at 0.23); the full unit suite runs once, at the final head after this amendment (`tmp/fix024/unit.log`), its figure in the delivery report. **Measured at 0.25**: `ruff check` and `ruff format --check` (`python -P -m ruff`) clean, 707 files, at `192682d`; `repoach arch check --base origin/develop` rc 0, `edge-honesty: ok`; `repoach arch graph --check` the same cycle and conflict lines as `origin/develop`, 2 diff lines as at 0.24 (`tmp/fix025/gates_light.log`); the full unit suite runs once, at the final head after this amendment (`tmp/fix025/unit.log`), its figure in the delivery report. **Measured at 0.26**: `ruff check` and `ruff format --check` (`python -P -m ruff`) clean, 707 files; `repoach arch check --base origin/develop` rc 0, `edge-honesty: ok`; `repoach arch graph --check` the same lines as at 0.25 (`tmp/fix026/gates_light.log`); the full unit suite runs once, at the final head after this amendment (`tmp/fix026/unit.log`), its figure in the delivery report.
- [x] AC6: every symbol of **What 0.17 removes or demotes** marked delete is absent from `src/` and `scripts/` (`git grep -n <symbol>` → 0), every retired test named in its deleting commit, and no new regex of `recording.py` or the scripts recognises a `//`, `glab api` or mailbox shape for safety.
- [x] AC7: `docs/security_posture.md` states D7, D8, D9 and the deletion in its own `docs(security)` commit; the spec is bumped once, in one `docs(specs)` commit.
- [x] AC8 (0.20): the class sweep `tests/unit/test_forge_recorder_sweep.py` — every lead, token kind, follow, encoding and position of every rule of D7, D8, D1b and D2, 46 realistic carriers in seven host classes, 40 ordinary lines in four positions, each cell's expectation computed from the rule set — gives, run whole by `tmp/fable020/sweep.py` at `fca0300`: 4 088 cells, 0 crashes, 0 leaks, 0 over-refusals, 0 corruptions, 0 offline parity breaks (hosts 959, ipv4 965, ipv6 630, decoded 545, positions 166, known 149, mailbox 105, mentions 112, carriers 457); at `2322070` plus the stopped implementer's edits it gave 215 failing cells in the fifteen classes of `tmp/fable020/AUDIT.md`. A deterministic fifth of it (at least 500 cells, by a stable hash of the id) runs in the unit suite. The crash probe `tmp/fable020/crash_probe.py` (sixteen hostile but accidental shapes) raises nothing and runs the oracle alone on 5 000 URLs in 0.15 s and on a 1.2 MB diff in 1.3 s (`record()` itself is slower by the cost of `residual`, 0.21 (e)). **0.21** (`2be2cea`, `tmp/fable021/sweep.py`): 5 148 cells in twelve rules, 0 crashes, 0 leaks, 0 over-refusals, 0 corruptions, 0 offline parity breaks (the nine rules of 0.20 unchanged; routes 157, logins 849, nesting 54); the 0.20 verifier's own sweep `tmp/verify020/sweep_v2.py` rerun: 11 584 cells in 90 s, 0 leaks, 0 crashes, 0 cells slower than 1 s, and the 17 findings it had outside B1 to B3 unchanged and deferred by the owner (13 `home_path` over-refusals of CI runner homes, 3 `*.svc.cluster.local:8080` rewrites by the followed-host rule, 1 stdin parity case); its hostile and scaling probes: 0 crashes, the 20 000-`[` body written as text. **0.22** (`d00ea81`, the code unchanged since `a86f418`; `tmp/fable022/sweep.py`): 6 174 cells in twelve rules, 0 crashes, 0 leaks, 0 over-refusals, 0 corruptions, 0 offline parity breaks (the eleven other rules unchanged; routes 1 183: 26 route grammars, 42 reserved words of four forges in two shapes, five tarball shapes on ten hosts); the same `routes` dimension run against the 0.21 code `188d732` (`tmp/fable022/sweep_base.log`): 594 failing cells = 46 leaks (the Web IDE in five shapes, the team slug and its API shape, Bitbucket Server's personal repository) + 548 corruptions (474 reserved words, 70 tarballs, 4 Bitbucket Server project repositories). The 0.20 verifier's `tmp/verify020/sweep_v2.py` rerun: 11 584 cells in 78 s, 0 leaks, 0 crashes, 0 cells slower than 1 s, the same 17 deferred findings (13 `home_path` over-refusals, 3 `svc.cluster.local` rewrites, 1 stdin parity case); the 0.21 verifier's `tmp/verify021/cells.py` on the head: 91 cells, **0 leaks** (0.21: 4), 0 over-refusals, 0 crashes, 7 cells changed by the instance-host rewrite or the deferred Gitea `user` route, 6 route words lost in the deferred Bitbucket classes, 5 harness parity cells of B2 unchanged; `tmp/verify021/hostile.py`: 181 cells, 0 crashes, 32 named refusals; `tmp/verify020/repro_blocking.py`: L1 closed, L2 written as NG4-13 says. **0.23** (`2de9f27`; `tmp/fable023/sweep.py`): 6 257 cells in twelve rules, 0 crashes, 0 leaks, 0 over-refusals, 0 corruptions, 0 offline parity breaks (the eleven other rules unchanged; routes 1 266: the five GitLab npm registry shapes on the five host classes and 33 `remote:<word>` cells, two of them refused as `needle segment`). The 0.20 verifier's `tmp/verify020/sweep_v2.py` rerun: 11 584 cells in 80 s, 0 leaks, 0 crashes, the same 17 deferred findings line for line (13 `home_path` over-refusals, 3 `svc.cluster.local` rewrites, 1 stdin parity case); the 0.21 verifier's `tmp/verify021/cells.py`: 91 cells, identical to its 0.22 line (0 leaks); the 0.22 verifier's `tmp/verify022/cells22.py`: 145 cells, 14 leaks (0.22: 16), 1 kept word lost (Bitbucket Server `scm/~<user>`, deferred), 0 over-refusals, 0 corruptions, 0 crashes, **0 parity breaks** (0.22: 1, the `snippets/<repo>` remote) — the 14: the seven identity slots after an owner root (six GitHub, one GitLab `users/<u>/-/repos/<r>`) out of scope by the owner's brief (deferred), the five GitLab and GitHub Packages cells whose only surviving marker is the package name the cell itself lists as kept, `t_private_scoped_scope_kept` (NG4-15) and `t_not_a_tarball_group_route` (a name after `/-/`, the route by 0.21 B1; deferred as a cell question); the 017, 018 and 019 probe sets identical to their 0.22 run line for line; `repro_blocking.py` identical; `hostile.py` 181 cells, 0 crashes, 32 named refusals, only its timings differ. **0.24** (`19878b3`; `tmp/fix024/sweep.py`): 6 475 cells in twelve rules, 0 crashes, 0 leaks, 0 over-refusals, 0 corruptions, 0 offline parity breaks (the eleven other rules unchanged; routes 1 484: 99 nested remote cells, 99 argv API path cells, two forwarded public scopes on five host classes; at the 0.23 code the new tests and rows fail: `tmp/fix024/b023`). The 0.20 verifier's `tmp/verify020/sweep_v2.py` rerun: 11 584 cells, 0 leaks, 0 crashes, the same 17 deferred findings line for line; the 0.22 verifier's `cells22.py`: 145 cells, identical verdicts to its 0.23 run (14 leaks, all deferred or NG4-15, 1 kept word lost, deferred, 0 parity breaks); the 0.23 verifier's `tmp/verify023/cells23.py`: 173 cells, **0 B1 cells failing** (0.23: 14 parity breaks, 8 leaks), 13 leaks that are NG4-15 by 0.24 (c) (a third party's scope on GitLab's endpoints, kept as text), 1 corruption and 1 parity break that are the same at 0.22 and are not this pass's classes (`b2_priv_enc_meta`, a lone segment on a private host written `org`, the same at 0.21; `n_priv_scope_is_participant_login`, the harness parity case the corpus's `_offline_parity_possible` excludes), both measured and appended to `tmp/fable021/DEFERRED.md`. **Measured at 0.25** (`192682d`; `tmp/fix024/sweep.py`): 6 475 cells, 0 failures (routes 1 484); `tmp/verify020/sweep_v2.py`: the same 17 deferred findings line for line; the cells of every earlier verifier against `20de0b5` (`tmp/fix025/allcells.sh`, `tmp/fix025/cmp.py`): the 0.21 verifier's 91, the 0.22 verifier's 145 and the 0.23 verifier's 173 cells identical verdicts; the 0.24 verifier's 115 cells: 0 leaks, the four corruptions of 0.25 (a) ok, the 7 corruptions and 2 harness parity cells deferred above unchanged, the `repos/frontend` cell written as it stands. **Measured at 0.26** (`tmp/fix024/sweep.py` on the final code): 6 475 cells, 0 failures (routes 1 484); `tmp/verify020/sweep_v2.py`: the same 17 deferred findings line for line; the cumulative bank against `fb2bf94` and `20de0b5` (`tmp/fix026/bank.sh`, `tmp/fix026/bankcmp.py`): the 0.21 verifier's 91, the 0.22 verifier's 145 and the 0.23 verifier's 173 cells identical verdicts on both; the 0.24 verifier's 115 cells identical to `fb2bf94` (4 corruptions fewer than `20de0b5`, as at 0.25); the 0.25 verifier's 155 cells: 0 LEAK and 0 CORRUPTION (34 LEAK at `fb2bf94`, 40 CORRUPTION at `20de0b5`), 6 PARITY, the participant-login cells `L_*_participant_repo` of the deferred harness class (the cell's offline half reads a raw text holding a login only the live recorder learns; the same 6 at `20de0b5`, 3 at `fb2bf94` where the other 3 leaked); the differential gate (`tmp/verify025/diffgate.py`, 1 550 inputs): 0 corpus row changed, 35 cells of the 0.25 verifier changed (the 34 leaks closed and `L_argv_eq_endpoint`, an INFO cell whose `--input=repos/<o>/<r>/pulls/…` is now replaced), 10 new rows (`tmp/fix026/diffcmp.log`). **Measured at 0.27** (`346cc8c`; `tmp/fix024/sweep.py`): 6 475 cells, 0 failures (routes 1 484); the 0.26 verifier's `cells26.py` 204 cells: 0 CORRUPTION (74 at `0d604fe`), 48 LEAK, every one in prose (NG4-16 by `tmp/fix027/attrib.py`), 4 PARITY of the deferred participant-login class; `cells26b.py` 21 cells: 0 CORRUPTION, 7 LEAK all NG4-16; the 0.25 verifier's `cells25.py` 155 cells: 0 CORRUPTION, 12 LEAK all NG4-16, 5 PARITY (the participant class); the differential gate against `0d604fe` (`tmp/fix027/diffgate27.py`, 1 787 inputs): 0 corpus row changed but the four renamed, 141 cells changed, every one attributed (`tmp/fix027/attrib_diff.log`: 74 corruptions closed, 67 NG4-16 prose leaks), 16 new rows.

## Architecture Impact

- Adds module: `src/repoach/forge/closure.py` (under the `src/repoach/forge/` directory SP-UNIVERSAL-FORGE owns, so it is not listed in this spec's `owns.code`, which the architecture gate would report as a second owner; tested by this spec; imports the standard library only).
- Adds dependency: `src/repoach/forge/recording.py` → `src/repoach/forge/closure.py` (D7 source 3, the placeholder and public predicates). The reverse edge is forbidden by test (AC2).
- Removes coupling: the recorder script no longer carries the gate's `mailbox` and `public_ipv4` copies (two of the three 0.8 copies); `HOME_PATH_RE` and the documentation-domain list remain, the latter now in `closure.py`.
- Cycles and ownership conflicts: unchanged (27 and 5 at `origin/develop` `8ee2c38`, re-measured under AC5: the same lines).

## Diagram

```mermaid
flowchart LR
    P[real PR, gh/glab answers] --> K[known_values: structured fields + deny-list + closure.hosts over raw text → K]
    P --> R[redact_payload: keys dropped, user name by context]
    R --> C[convenience: redact_url, SCP, noreply, classify_logins, ids synthetic]
    K --> T[replace_known: every K value → class token, every string]
    C --> T
    T --> O{closure: residual K? unknown host? public address? unknown mailbox? + home path, deny-list}
    O -- yes --> E[exit 2: reason, kind, ordinal, path]
    O -- no --> F[fixture written]
```

## Open Questions

- O1, O2, Q1–Q4 (0.6–0.10): resolved as recorded at 0.16.
- Q5 (0.17, owner, resolved 2026-11-22): the placeholders stay on `forge.example.com`, and SP-NO-PERSONAL-DATA-GATE will accept `example.com` and all its subdomains (RFC 2606). The question as asked: SP-NO-PERSONAL-DATA-GATE D1's documentation-domain rule, copied here, accepts `example.com` exactly and `.example` suffixes; the recorder's noreply placeholders `author@users.noreply.forge.example.com` / `bot@…` / `noreply@forge.example.com` are on `forge.example.com`, which that rule does not accept. Either the gate adds `forge.example.com` and its subdomains to its documentation domains when it lands, or the placeholders move to `author@users.noreply.example.com`. The oracle treats the four placeholders as literals meanwhile (D8).
- Q6 (0.17, owner, resolved 2026-11-22: the explicit list): `HOST_TLDS` is an explicit list (ISO 3166-1 alpha-2 plus the generic TLDs named in D8). The alternative is the full IANA root zone (about 1 500 entries) committed as a data file and refreshed by hand; it would catch a self-hosted forge on an exotic new TLD cited un-led, at the cost of a file to maintain. Default if unanswered: the explicit list; an exotic-TLD host is learnt when led or structured.
- Q7 (0.17, owner, resolved 2026-11-22: five digits): D9's threshold is five digits, so a self-hosted project id below 10 000 cited only in text is written (NG4-6). Lowering it to four digits refuses or rewrites every `projects/<year>/…` path of 2020–2030 (R8-D2, the round-8 over-refusal). Default if unanswered: five.
