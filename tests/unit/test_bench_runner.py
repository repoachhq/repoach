"""``repoach bench run``: cases reviewed through a fake review, ledger cases, partial runs."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from repoach.bench.corpus import load_corpus
from repoach.bench.runner import (
    BenchRunner,
    BenchUnavailableError,
    ReviewRequest,
    materialise_local,
    pr_url,
)
from repoach.cli.main import app
from repoach.forge.protocol import ForgeError
from repoach.providers.errors import ProviderConfigError
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    init_findings_schema,
    record_finding,
)
from repoach.review.orchestrator import TeamOutcome
from repoach.review.persistence import init_schema, record_review
from repoach.review.reviewer import BotRole, ReviewComment, ReviewerOutcome, ReviewVerdict

REPO = Path(__file__).resolve().parents[2]
PLANTED = REPO / "bench" / "corpus" / "planted"


def _finding(pr_number: int, head_sha: str, *, status: FindingStatus, line: int) -> Finding:
    return Finding(
        pr_number=pr_number,
        head_sha=head_sha,
        round=1,
        finder="sentinel",
        claim_type=ClaimType.SECURITY,
        severity=Severity.BLOCKING,
        file="src/inventory/store.py",
        line_start=line,
        line_end=line,
        claim="SQL built from the search fragment",
        evidence_pointer="src/inventory/store.py",
        status=status,
        verification_result="judged",
    )


def ready() -> None:
    """Stand in for the provider preflight: the fake review needs no provider."""


def test_unready_provider_is_named_before_any_case() -> None:
    def unready() -> None:
        raise ProviderConfigError("no key")

    corpus = load_corpus(PLANTED / "01-security-sql-injection.yaml")
    with pytest.raises(BenchUnavailableError, match="provider fake: no key"):
        BenchRunner(corpus, provider="fake", review=fake_review, preflight=unready).run()


def fake_review(request: ReviewRequest) -> TeamOutcome:
    """Record one blocker finding on the planted SQL line, plus an older-head row."""
    assert "LIKE '%{fragment}%'" in request.forge.diff(request.pr_number).text
    init_findings_schema(request.db_path)
    record_finding(
        request.db_path,
        _finding(request.pr_number, request.head_sha, status=FindingStatus.VERIFIED, line=60),
    )
    stale = _finding(request.pr_number, request.head_sha, status=FindingStatus.REFUTED, line=60)
    record_finding(request.db_path, stale.model_copy(update={"head_sha": "0" * 40}))
    comment = ReviewComment(
        file="src/inventory/store.py",
        line=60,
        severity="blocker",
        body="SQL built from the search fragment",
    )
    outcome = ReviewerOutcome(
        role=BotRole.SENTINEL,
        verdict=ReviewVerdict.REQUEST_CHANGES,
        summary="",
        comments=[comment],
    )
    return TeamOutcome(
        pr_number=request.pr_number,
        final_verdict=ReviewVerdict.REQUEST_CHANGES,
        n_blockers=1,
        n_majors=0,
        reviews=[outcome],
    )


def test_local_case_is_reviewed_and_recorded_at_its_head() -> None:
    corpus = load_corpus(PLANTED / "01-security-sql-injection.yaml")
    result = BenchRunner(corpus, provider="fake", review=fake_review, preflight=ready).run()
    assert not result.partial
    (run,) = result.cases
    assert run.head_sha is not None
    assert run.pr_url is None
    (finding,) = run.findings
    assert finding.severity == "blocker"
    assert finding.status == "verified"
    assert finding.reviewer == "sentinel"
    assert run.verdict == "REQUEST_CHANGES"


def test_reviewers_filter_drops_other_findings() -> None:
    corpus = load_corpus(PLANTED / "01-security-sql-injection.yaml")
    result = BenchRunner(
        corpus, provider="fake", reviewers=["tester"], review=fake_review, preflight=ready
    ).run()
    assert result.cases[0].findings == []


def test_dry_run_calls_no_review() -> None:
    def boom(request: ReviewRequest) -> TeamOutcome:
        raise AssertionError("a dry run reviewed")

    corpus = load_corpus(PLANTED / "01-security-sql-injection.yaml")
    result = BenchRunner(corpus, provider="fake", review=boom, dry_run=True).run()
    assert result.dry_run
    assert result.cases[0].head_sha is not None
    assert result.cases[0].findings == []


def test_failing_case_stops_the_run_as_partial(tmp_path: Path) -> None:
    calls: list[int] = []

    def flaky(request: ReviewRequest) -> TeamOutcome:
        calls.append(request.pr_number)
        if len(calls) == 2:
            raise ForgeError("provider went away")
        return fake_review(request)

    corpus = load_corpus(PLANTED)
    result = BenchRunner(corpus, provider="fake", review=flaky, preflight=ready).run()
    assert result.partial
    assert [c.case for c in result.cases] == ["01-security-sql-injection"]


def test_ledger_case_reads_recorded_findings_and_comment_severity(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    init_findings_schema(db)
    init_schema(db)
    record_finding(db, _finding(296, "a" * 40, status=FindingStatus.REFUTED, line=12))
    record_review(
        db,
        pr_number=296,
        outcome=ReviewerOutcome(
            role=BotRole.SENTINEL,
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary="",
            comments=[
                ReviewComment(
                    file="src/inventory/store.py",
                    line=12,
                    severity="major",
                    body="SQL built from the search fragment",
                )
            ],
        ),
    )
    case = tmp_path / "corpus"
    case.mkdir()
    (case / "mr-296.yaml").write_text(
        yaml.safe_dump(
            {
                "case": "mr-296",
                "private": True,
                "source": {
                    "kind": "ledger",
                    "repo": "https://gitlab.com/o/r",
                    "pr": 296,
                    "head_sha": "a" * 40,
                },
            }
        ),
        encoding="utf-8",
    )
    result = BenchRunner(load_corpus(case), provider="ledger", ledger_db=db).run()
    (run,) = result.cases
    assert run.pr_url == "https://gitlab.com/o/r/-/merge_requests/296"
    (finding,) = run.findings
    assert (finding.severity, finding.status) == ("major", "refuted")


def test_pr_url_follows_the_forge_of_the_host() -> None:
    assert pr_url("https://github.com/o/r.git", 3) == "https://github.com/o/r/pull/3"
    assert pr_url("git@gitlab.com:o/r.git", 4) == "https://gitlab.com/o/r/-/merge_requests/4"


def test_cli_run_dry_run_writes_the_result_file(tmp_path: Path) -> None:
    out = tmp_path / "results"
    result = CliRunner().invoke(
        app,
        [
            "bench",
            "run",
            "--corpus",
            str(PLANTED / "03-logic-discount-inverted.yaml"),
            "--provider",
            "claude_code",
            "--dry-run",
            "--results-dir",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    (written,) = out.iterdir()
    assert written.name.endswith("_03-logic-discount-inverted_claude_code.json")
    data = json.loads(written.read_text(encoding="utf-8"))
    assert data["dry_run"] is True


def test_cli_run_unknown_corpus_exits_two(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["bench", "run", str(tmp_path / "none")])
    assert result.exit_code == 2


@pytest.mark.parametrize("reviewers", ["nobody", "architect,ghost"])
def test_cli_run_unknown_reviewer_exits_two(reviewers: str) -> None:
    result = CliRunner().invoke(
        app, ["bench", "run", str(PLANTED), "--reviewers", reviewers, "--dry-run"]
    )
    assert result.exit_code == 2


def test_shipped_planted_corpus_is_valid() -> None:
    corpus = load_corpus(REPO / "bench" / "corpus" / "planted")
    assert corpus.name == "planted"
    assert len(corpus.cases) == 10
    kinds = {defect.kind for case in corpus.cases for defect in case.planted}
    assert kinds == {"security", "logic", "test_gap", "docstring", "convention"}
    assert all(len(case.planted) == 1 for case in corpus.cases)
    assert len(corpus.sha) == 12


def _added_lines(tree: Path, base: str, head: str, file: str) -> set[int]:
    diff = subprocess.run(
        ["git", "diff", "-U0", base, head, "--", file],
        cwd=tree,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    added: set[int] = set()
    for match in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff, re.MULTILINE):
        start, count = int(match.group(1)), int(match.group(2) or "1")
        added.update(range(start, start + count))
    return added


def test_every_planted_range_covers_a_line_its_patch_adds(tmp_path: Path) -> None:
    corpus = load_corpus(REPO / "bench" / "corpus" / "planted")
    for case in corpus.cases:
        workdir = tmp_path / case.case
        workdir.mkdir()
        tree, base, head = materialise_local(case, workdir)
        defect = case.planted[0]
        first, last = defect.lines
        added = _added_lines(tree, base, head, defect.file)
        assert added & set(range(first, last + 1)), case.case


def test_ledger_case_reads_rows_recorded_with_no_head(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    init_findings_schema(db)
    record_finding(db, _finding(7, "", status=FindingStatus.PROPOSED, line=3))
    record_finding(db, _finding(7, "e" * 40, status=FindingStatus.PROPOSED, line=9))
    (tmp_path / "c.yaml").write_text(
        yaml.safe_dump(
            {
                "case": "c",
                "source": {
                    "kind": "ledger",
                    "repo": "https://github.com/o/r",
                    "pr": 7,
                    "head_sha": "unrecorded",
                },
            }
        ),
        encoding="utf-8",
    )
    result = BenchRunner(load_corpus(tmp_path / "c.yaml"), provider="x", ledger_db=db).run()
    assert [f.line_start for f in result.cases[0].findings] == [3]
