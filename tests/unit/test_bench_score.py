"""Scoring arithmetic of ``repoach bench`` (SP-PRECISION-MEASUREMENT AC2, AC3, AC5).

The fixture holds seven findings and two planted defects whose outcome
was worked out by hand:

* f1 architect blocker, label true, verified, overlaps defect A → TP, A found
* f2 sentinel major, label true, open → TP
* f3 tester minor, label true, proposed → TP
* f4 scribe nit, label false, verified → FP
* f5 sentinel major, label false, refuted → correct refutation
* f6 architect blocker, label true, refuted, overlaps defect B → wrong
  refutation; B is not found (its only finding was refuted)
* f7 tester minor, label unsure → unsure

precision 3/(3+1), refuter precision 1/(1+1), recall 1/2, unsure 1.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from repoach.bench.corpus import (
    Case,
    CaseSource,
    Corpus,
    LabelledFinding,
    PlantedDefect,
    fingerprint,
)
from repoach.bench.result import BenchResult, CaseRun, RecordedFinding
from repoach.bench.score import (
    Ratio,
    ReadmeLineRefusedError,
    readme_line,
    render_report,
    score,
    totals_line,
)

README_LINE = re.compile(
    r"precision [0-9.]+ \([0-9]+/[0-9]+\), recall [0-9.]+ \([0-9]+/[0-9]+\) on [0-9]+ "
    r"labelled findings across [0-9]+ cases, corpus .+@[0-9a-f]{7,}, measured "
    r"20[0-9-]{8}, provider .+"
)


def _finding(
    name: str, reviewer: str, severity: str, status: str, line: int, file: str = "src/a.py"
) -> RecordedFinding:
    return RecordedFinding(
        fingerprint=fingerprint(
            reviewer=reviewer, claim_type="broken_behavior", file=file, line_start=line
        ),
        reviewer=reviewer,
        claim_type="broken_behavior",
        severity=severity,
        file=file,
        line_start=line,
        line_end=line,
        claim=f"claim {name}",
        status=status,
    )


FINDINGS = {
    "f1": _finding("f1", "architect", "blocker", "verified", 10),
    "f2": _finding("f2", "sentinel", "major", "open", 30),
    "f3": _finding("f3", "tester", "minor", "proposed", 40),
    "f4": _finding("f4", "scribe", "nit", "verified", 50),
    "f5": _finding("f5", "sentinel", "major", "refuted", 60),
    "f6": _finding("f6", "architect", "blocker", "refuted", 100),
    "f7": _finding("f7", "tester", "minor", "proposed", 70),
}
LABELS = {
    "f1": "true",
    "f2": "true",
    "f3": "true",
    "f4": "false",
    "f5": "false",
    "f6": "true",
    "f7": "unsure",
}


def _corpus(labels: dict[str, str] | None = None, *, repeat: int = 1) -> Corpus:
    entries = [
        LabelledFinding(
            fingerprint=FINDINGS[name].fingerprint,
            label=label,
            labeller="owner",
            labelled_on=date(2026, 10, 21),
        )
        for name, label in (labels if labels is not None else LABELS).items()
    ]
    cases = [
        Case(
            case=f"case-{i}",
            source=CaseSource(kind="local", fixture="sample", patch="p.patch"),
            planted=[
                PlantedDefect(file="src/a.py", lines=(9, 12), kind="logic", severity="blocker"),
                PlantedDefect(file="src/a.py", lines=(100, 101), kind="security", severity="major"),
            ],
            findings=entries,
        )
        for i in range(repeat)
    ]
    return Corpus(name="fixture", root=Path("/corpus"), sha="0123456789ab", cases=cases)


def _result(*, repeat: int = 1, partial: bool = False) -> BenchResult:
    return BenchResult(
        corpus="fixture",
        corpus_root="/corpus",
        corpus_sha="0123456789ab",
        provider="claude_code",
        reviewers=["architect", "sentinel", "tester", "scribe"],
        measured_on=date(2026, 10, 21),
        started_at=datetime(2026, 10, 21, tzinfo=UTC),
        partial=partial,
        cases=[
            CaseRun(case=f"case-{i}", kind="local", findings=list(FINDINGS.values()), tokens=10)
            for i in range(repeat)
        ],
    )


def test_totals_match_the_hand_count() -> None:
    card = score(_result(), _corpus())
    t = card.total
    assert (t.tp, t.fp, t.unsure, t.unlabelled) == (3, 1, 1, 0)
    assert (t.correct_refutations, t.wrong_refutations) == (1, 1)
    assert (t.planted, t.found) == (2, 1)
    line = totals_line(card)
    assert "precision 3/4" in line
    assert "refuter precision 1/2" in line
    assert "recall 1/2" in line
    assert "unsure 1" in line
    assert t.precision.text() == "0.75 (3/4)"
    assert card.planted_located == 1


def test_per_reviewer_rows() -> None:
    rows = score(_result(), _corpus()).by_reviewer
    assert list(rows) == ["architect", "sentinel", "tester", "scribe"]
    assert rows["architect"].cells() == ["1", "0", "0", "1.00 (1/1)", "0.00 (0/1)", "0.50 (1/2)"]
    assert rows["sentinel"].cells() == ["1", "0", "0", "1.00 (1/1)", "1.00 (1/1)", "0.00 (0/2)"]
    assert rows["tester"].cells() == ["1", "0", "1", "1.00 (1/1)", "cannot say", "0.00 (0/2)"]
    assert rows["scribe"].cells() == ["0", "1", "0", "0.00 (0/1)", "cannot say", "0.00 (0/2)"]


def test_per_severity_rows_key_recall_by_planted_severity() -> None:
    rows = score(_result(), _corpus()).by_severity
    assert list(rows) == ["blocker", "major", "minor", "nit"]
    assert rows["blocker"].cells() == ["1", "0", "0", "1.00 (1/1)", "0.00 (0/1)", "1.00 (1/1)"]
    assert rows["major"].cells() == ["1", "0", "0", "1.00 (1/1)", "1.00 (1/1)", "0.00 (0/1)"]
    assert rows["minor"].cells() == ["1", "0", "1", "1.00 (1/1)", "cannot say", "cannot say"]
    assert rows["nit"].cells() == ["0", "1", "0", "0.00 (0/1)", "cannot say", "cannot say"]


def test_report_header_is_the_spec_header() -> None:
    lines = render_report(score(_result(), _corpus()))
    assert "reviewer | TP | FP | unsure | precision | refuter precision | recall" in lines
    assert "severity | TP | FP | unsure | precision | refuter precision | recall" in lines


def test_markdown_report_is_a_table() -> None:
    lines = render_report(score(_result(), _corpus()), markdown=True)
    assert "| reviewer | TP | FP | unsure | precision | refuter precision | recall |" in lines
    assert "| --- | ---: | ---: | ---: | ---: | ---: | ---: |" in lines


def test_unlabelled_findings_are_excluded_and_listed() -> None:
    card = score(_result(), _corpus({"f1": "true"}))
    assert card.total.tp == 1
    assert card.total.unlabelled == 6
    assert len(card.unlabelled) == 6
    assert card.total.precision.fraction() == "1/1"


def test_two_findings_on_one_defect_count_it_once() -> None:
    result = _result()
    twin = _finding("f8", "sentinel", "blocker", "verified", 11)
    result.cases[0].findings.append(twin)
    corpus = _corpus()
    corpus.cases[0].findings.append(
        LabelledFinding(fingerprint=twin.fingerprint, label="true", labeller="owner")
    )
    card = score(result, corpus)
    assert card.total.found == 1
    assert card.total.tp == 4


def test_skipped_case_is_counted_not_scored() -> None:
    result = _result()
    result.cases.append(CaseRun(case="gone", kind="forge", skipped="head moved"))
    card = score(result, _corpus())
    assert card.cases_skipped == 1
    assert card.cases_scored == 1
    assert card.total.tp == 3


def test_ratio_with_zero_denominator_cannot_say() -> None:
    assert Ratio(0, 0).text() == "cannot say"
    assert Ratio(0, 0).fraction() == "cannot say"


def test_label_match_rate_flags_stale_labels() -> None:
    result = _result()
    result.cases[0].findings = result.cases[0].findings[:2]
    card = score(result, _corpus())
    assert card.match_rate.fraction() == "2/7"
    assert card.labels_stale


def test_readme_line_refused_below_one_hundred_labels() -> None:
    card = score(_result(), _corpus())
    with pytest.raises(ReadmeLineRefusedError, match=r"corpus too small: 7 < 100"):
        readme_line(card)


def test_readme_line_refused_on_a_partial_run() -> None:
    card = score(_result(repeat=15, partial=True), _corpus(repeat=15))
    with pytest.raises(ReadmeLineRefusedError, match="partial"):
        readme_line(card)


def test_readme_line_matches_the_published_pattern() -> None:
    card = score(_result(repeat=15), _corpus(repeat=15))
    line = readme_line(card)
    assert README_LINE.fullmatch(line), line
    assert line.startswith("precision 0.75 (45/60), recall 0.50 (15/30) on 105 labelled")
