"""Common words and fully served identifiers never defer a refuter verdict.

Measured on PR #260 (2026-10-26, findings 41 and 66): two correct
refutations were deferred at every head because the judge's reasoning
named ``except``, ``Exception``, ``return``, ``None``,
``coverage_rows_at_head``, ``message``, ``https``, ``access`` and
``token``, all capped at the 50-hit limit and so "partly served". These
tests pin SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS: the stop-list (G1),
the cited-file rule (G2), the re-judging of a ``needs judgement`` stamp
(G3) and the reason shapes (G4).
"""

from __future__ import annotations

import builtins
import keyword
from collections.abc import Callable
from pathlib import Path

import pytest

from repoach.gates.config import REFUTER_CONTEXT_TOKENS_FLOOR, GatesConfig
from repoach.review import head_reverify, refuter_evidence
from repoach.review.findings import (
    Finding,
    FindingStatus,
    fetch_findings,
    init_findings_schema,
    record_finding,
    record_verification_attempt,
)
from repoach.review.hallucination_guard import make_repo_file_reader
from repoach.review.head_reverify import (
    NEEDS_JUDGEMENT_METHOD,
    judgement_key,
    needs_judgement_stamp,
    reverify_older_findings,
    stamp_key,
)
from repoach.review.refuter import (
    DEFERRAL_REASONS,
    EVIDENCE_NOT_SERVED,
    EVIDENCE_PARTLY_SERVED_IN_CITED,
    EVIDENCE_PARTLY_SERVED_OUTSIDE,
    _partial_veto,
    judge_finding,
)
from repoach.review.refuter_evidence import (
    REFUTER_RULE_VERSION,
    STOP_WORDS,
    EvidenceBundle,
    _Assembly,
    _classify,
    build_evidence,
    claim_identifiers,
    defined_names,
    is_stop_word,
)
from tests.refuter_fakes import blocking_finding
from tests.refuter_fakes import git_tree as _git_tree
from tests.refuter_fakes import judge_reply as _reply

MEASURED_COMMON_WORDS = (
    "except",
    "Exception",
    "return",
    "None",
    "message",
    "https",
    "access",
    "token",
)
"""The eight common words of findings 41 and 66 the veto held on (PR #260)."""

CLAIM_41 = (
    "CONFIRM: the excerpt at 1250-1257 shows `except Exception` → log `spec_coverage_skipped` "
    "→ `return None`, so no row is recorded. `coverage_rows_at_head` now runs `_readable_base`, "
    "`fetch_base` and `specs_owning` → `git show` inside this try."
)
CLAIM_66 = (
    "CONFIRM. Excerpt line 1255 logs `message=str(exc)[:200]` at WARNING before "
    "`_record_grading_failure` applies `_redacted`. The failed fetch can echo "
    "`https://x-access-token:ghs_…@github.com` so a forge token leaks."
)
CITED = "src/orch.py"
HEAD_A = "a" * 40
HEAD_B = "b" * 40


def _finding(claim: str, *, file: str = CITED, line: int = 2, head: str = HEAD_A) -> Finding:
    return blocking_finding(claim, file=file, line=line, head=head, pr_number=260)


def _bundle(root: Path, claim: str, *, budget: int = 200_000) -> EvidenceBundle:
    source = (root / CITED).read_text(encoding="utf-8")
    bundle = build_evidence(
        source,
        repo_root=root,
        reader=make_repo_file_reader(root),
        path=CITED,
        line_start=2,
        line_end=2,
        claim=claim,
        budget_tokens=budget,
    )
    assert bundle is not None
    return bundle


def test_stop_list_holds_python_names_common_tokens_and_english_words() -> None:
    assert set(keyword.kwlist) <= STOP_WORDS
    assert {n for n in dir(builtins) if not n.startswith("_")} <= STOP_WORDS
    assert {"https", "self", "null", "message", "access", "token"} <= STOP_WORDS
    assert is_stop_word("Message") and is_stop_word("HTTPS")
    assert not is_stop_word("coverage_rows_at_head")
    assert not is_stop_word("AccessToken")
    assert REFUTER_RULE_VERSION == 2


@pytest.mark.parametrize("word", MEASURED_COMMON_WORDS)
def test_measured_common_words_are_never_identifiers(word: str) -> None:
    assert is_stop_word(word)
    for claim in (CLAIM_41, CLAIM_66, f"`{word}` is wrong here"):
        identifiers, _ = claim_identifiers(claim, cited_file=CITED)
        assert word not in identifiers


def test_measured_claims_keep_their_real_identifiers() -> None:
    ids_41, _ = claim_identifiers(CLAIM_41, cited_file=CITED)
    ids_66, _ = claim_identifiers(CLAIM_66, cited_file=CITED)
    assert ids_41 == (
        "spec_coverage_skipped",
        "coverage_rows_at_head",
        "_readable_base",
        "fetch_base",
        "specs_owning",
    )
    assert ids_66 == ("_record_grading_failure", "_redacted", "ghs_")


def test_identifiers_shorter_than_four_characters_are_dropped() -> None:
    identifiers, _ = claim_identifiers("`abc` and `a_b` and `abcd_e` drift", cited_file=CITED)
    assert identifiers == ("abcd_e",)


def test_measured_words_never_veto_a_refutation(tmp_path: Path) -> None:
    body = "".join(
        f"def f_{n}(message, token):\n    try:\n        return None\n    except Exception:\n"
        f"        return 'https://access'\n"
        for n in range(30)
    )
    files = {f"src/other_{n:03}.py": body for n in range(20)}
    cited = "def g(x):\n    return _redacted(str(x))\n"
    _git_tree(tmp_path, {CITED: cited, "src/red.py": "def _redacted(s):\n    return s\n", **files})
    reasoning = (
        "Line 2 now logs _redacted(message) in the except Exception branch and returns None; "
        "no https access token reaches the log."
    )
    judgement = judge_finding(
        _finding(CLAIM_66),
        repo_root=tmp_path,
        judge=lambda _p: _reply("refuted", reasoning),
        context_tokens=REFUTER_CONTEXT_TOKENS_FLOOR,
    )
    assert judgement.status is FindingStatus.REFUTED
    assert not set(MEASURED_COMMON_WORDS) & set(judgement.bundle.partial)


def _fully_served_in_cited_tree(root: Path) -> Path:
    cited = "def run():\n    rows = coverage_rows_at_head()\n    return rows\n"
    files = {
        f"src/user_{n:03}.py": "from x import coverage_rows_at_head\ncoverage_rows_at_head()\n"
        for n in range(80)
    }
    return _git_tree(root, {CITED: cited, **files})


def test_identifier_fully_served_in_the_cited_file_does_not_veto(tmp_path: Path) -> None:
    root = _fully_served_in_cited_tree(tmp_path)
    claim = "`coverage_rows_at_head` raises and the error is swallowed"
    bundle = _bundle(root, claim)
    assert bundle.partial == ()
    assert bundle.partial_elsewhere == ("coverage_rows_at_head",)
    assert "search coverage_rows_at_head: 110 hits over the 50-hit cap" in bundle.truncated
    judgement = judge_finding(
        _finding(claim),
        repo_root=root,
        judge=lambda _p: _reply("refuted", "coverage_rows_at_head is now inside a fail-closed try"),
    )
    assert judgement.status is FindingStatus.REFUTED


def test_identifier_absent_from_the_cited_file_and_cut_elsewhere_still_vetoes(
    tmp_path: Path,
) -> None:
    files = {f"src/user_{n:03}.py": "COMMON_TABLE = 1\n" for n in range(80)}
    _git_tree(tmp_path, {CITED: "a = 1\nb = 2\n", **files})
    claim = "`COMMON_TABLE` has a divergent copy"
    judgement = judge_finding(
        _finding(claim),
        repo_root=tmp_path,
        judge=lambda _p: _reply("refuted", "every COMMON_TABLE copy agrees"),
    )
    assert judgement.status is FindingStatus.PROPOSED
    assert judgement.reason == EVIDENCE_PARTLY_SERVED_OUTSIDE.format(identifiers="COMMON_TABLE")


def test_cut_inside_the_cited_file_is_classified_in_cited() -> None:
    assembly = _Assembly(budget=1)
    hits = [(CITED, n) for n in range(1, 11)] + [("src/o.py", 1)]
    _classify(
        assembly,
        "wanted",
        hits=hits,
        own=4,
        served=[(CITED, 5), (CITED, 6), ("src/o.py", 1)],
        cited_path=CITED,
    )
    assert assembly.partial == ["wanted"]
    assert assembly.partial_in_cited == ["wanted"]
    whole = _Assembly(budget=1)
    _classify(whole, "wanted", hits=hits, own=10, served=[], cited_path=CITED)
    assert (whole.partial, whole.partial_elsewhere, whole.unserved) == ([], ["wanted"], [])
    none = _Assembly(budget=1)
    _classify(none, "wanted", hits=[("src/o.py", 1)], own=0, served=[], cited_path=CITED)
    assert none.unserved == ["wanted"]


def test_deferral_reason_names_the_rule_that_held() -> None:
    bundle = EvidenceBundle(
        text="",
        reads=(),
        partial=("inside_one", "outside_one"),
        partial_in_cited=("inside_one",),
    )
    assert _partial_veto("inside_one and outside_one agree", bundle) == (
        "evidence outside window (identifier partly served in the cited file: inside_one)"
    )
    assert _partial_veto("outside_one agrees", bundle) == (
        "evidence outside window (identifier partly served outside the cited file: outside_one)"
    )
    assert _partial_veto("nothing named", bundle) == ""
    assert EVIDENCE_NOT_SERVED.format(identifiers="x_y") == (
        "evidence outside window (identifier not served: x_y)"
    )
    assert EVIDENCE_PARTLY_SERVED_IN_CITED in DEFERRAL_REASONS
    assert EVIDENCE_PARTLY_SERVED_OUTSIDE in DEFERRAL_REASONS


def _ledger(tmp_path: Path) -> tuple[Path, Path, int]:
    root = _git_tree(tmp_path / "repo", {CITED: "def g(x):\n    return x\n"})
    db = tmp_path / "review.db"
    init_findings_schema(db)
    finding_id = record_finding(db, _finding("`g_value` leaks the secret"))
    return root, db, finding_id


def _counting(
    calls: list[str], verdict: str = "cannot_judge"
) -> Callable[[], Callable[[str], str]]:
    def _factory() -> Callable[[str], str]:
        def _judge(prompt: str) -> str:
            calls.append(prompt)
            return _reply(verdict, "the call site is not shown")

        return _judge

    return _factory


def _reverify(db: Path, root: Path, head: str, calls: list[str], **kwargs: object):
    return reverify_older_findings(
        db,
        pr_number=260,
        repo_root=root,
        head_sha=head,
        ci_green=True,
        config=GatesConfig.builtin_default(),
        max_judged=5,
        judge_factory=_counting(calls),
        **kwargs,
    )


def test_needs_judgement_is_reused_only_under_the_same_key(tmp_path: Path) -> None:
    root, db, finding_id = _ledger(tmp_path)
    calls: list[str] = []
    first = _reverify(db, root, HEAD_B, calls)
    assert first.counts()["needs_judgement"] == 1 and len(calls) == 1
    row = next(f for f in fetch_findings(db, 260) if f.id == finding_id)
    assert row.verification_method == NEEDS_JUDGEMENT_METHOD
    assert f"({judgement_key(REFUTER_CONTEXT_TOKENS_FLOOR)}): " in row.verification_result
    _reverify(db, root, HEAD_B, calls)
    assert len(calls) == 1


def test_needs_judgement_is_re_judged_at_a_new_head(tmp_path: Path) -> None:
    root, db, _ = _ledger(tmp_path)
    calls: list[str] = []
    _reverify(db, root, HEAD_B, calls)
    _reverify(db, root, "c" * 40, calls)
    assert len(calls) == 2


def test_needs_judgement_is_re_judged_when_the_context_changes(tmp_path: Path) -> None:
    root, db, _ = _ledger(tmp_path)
    calls: list[str] = []
    _reverify(db, root, HEAD_B, calls)
    _reverify(db, root, HEAD_B, calls, context_tokens=80_000)
    assert len(calls) == 2


def test_needs_judgement_is_re_judged_on_a_rule_version_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db, _ = _ledger(tmp_path)
    calls: list[str] = []
    _reverify(db, root, HEAD_B, calls)
    monkeypatch.setattr(head_reverify, "REFUTER_RULE_VERSION", REFUTER_RULE_VERSION + 1)
    _reverify(db, root, HEAD_B, calls)
    assert len(calls) == 2


def test_stamp_written_before_the_rule_version_is_re_judged(tmp_path: Path) -> None:
    root, db, finding_id = _ledger(tmp_path)
    record_verification_attempt(
        db,
        finding_id,
        method=NEEDS_JUDGEMENT_METHOD,
        result=f"needs judgement at {HEAD_B[:7]}: evidence outside window",
        checked_at_sha=HEAD_B,
    )
    calls: list[str] = []
    _reverify(db, root, HEAD_B, calls)
    assert len(calls) == 1


def test_needs_judgement_is_re_judged_when_the_ceiling_changes(tmp_path: Path) -> None:
    root, db, _ = _ledger(tmp_path)
    calls: list[str] = []
    _reverify(db, root, HEAD_B, calls)
    _reverify(db, root, HEAD_B, calls, context_ceiling=120_000)
    assert len(calls) == 2


def test_defined_names_reads_definitions_not_keyword_arguments() -> None:
    source = (
        "class Adapter:\n"
        "    def call(self, prompt):\n"
        "        self.name = prompt\n"
        "        log(message=prompt)\n"
        "case: str = 'x'\n"
    )
    names = defined_names(source, "@@ -1 +1 @@\n+def without(x):\n+    return x\n")
    assert {"Adapter", "call", "name", "case", "without"} <= names
    assert "message" not in names


def test_defined_stop_word_stays_an_identifier_and_d2_applies(tmp_path: Path) -> None:
    cited = "class Provider:\n    def call(self, prompt):\n        return self.send(prompt)\n"
    users = {f"src/user_{n:03}.py": "def use(p):\n    p.call('x')\n" for n in range(80)}
    _git_tree(tmp_path, {CITED: cited, "src/plain.py": "x = 1\ny = 2\n", **users})
    claim = "`call` sends the prompt unredacted"
    assert claim_identifiers(claim, cited_file=CITED, defined=defined_names(cited))[0] == ("call",)
    bundle = _bundle(tmp_path, claim)
    assert "grep call: 81 hits (50 served)" in bundle.reads
    assert bundle.partial == ()
    assert bundle.partial_elsewhere == ("call",)
    judgement = judge_finding(
        _finding(claim),
        repo_root=tmp_path,
        judge=lambda _p: _reply("refuted", "call now passes the prompt through _redacted"),
    )
    assert judgement.status is FindingStatus.REFUTED
    elsewhere = build_evidence(
        "x = 1\ny = 2\n",
        repo_root=tmp_path,
        reader=make_repo_file_reader(tmp_path),
        path="src/plain.py",
        line_start=2,
        line_end=2,
        claim=claim,
        budget_tokens=200_000,
    )
    assert elsewhere is not None
    assert not any(read.startswith("grep call") for read in elsewhere.reads)
    assert claim_identifiers(claim, cited_file="src/plain.py")[0] == ()


def test_is_stop_word_reads_stop_words_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(refuter_evidence, "STOP_WORDS", frozenset())
    assert not any(refuter_evidence.is_stop_word(word) for word in MEASURED_COMMON_WORDS)


def test_stamp_is_written_and_read_by_one_format() -> None:
    key = judgement_key(40_000, 200_000)
    stamp = needs_judgement_stamp("abc1234", key, "evidence outside window (x: y)")
    assert stamp == f"needs judgement at abc1234 ({key}): evidence outside window (x: y)"
    assert stamp_key(stamp) == key
    assert stamp_key("needs judgement at abc1234: evidence outside window") is None
    assert stamp_key("still present at abc1234: confirmed") is None
