"""``repoach bench report|score|label`` on a recorded result (SP-PRECISION-MEASUREMENT AC1, AC5)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import yaml
from typer.testing import CliRunner

from repoach.bench.corpus import fingerprint, load_case
from repoach.bench.result import BenchResult, CaseRun, RecordedFinding, save_result
from repoach.cli.main import app


def _finding(reviewer: str, line: int, status: str) -> RecordedFinding:
    return RecordedFinding(
        fingerprint=fingerprint(
            reviewer=reviewer, claim_type="security", file="src/a.py", line_start=line
        ),
        reviewer=reviewer,
        claim_type="security",
        severity="blocker",
        file="src/a.py",
        line_start=line,
        line_end=line,
        claim=f"{reviewer} claim at {line}",
        status=status,
    )


def recorded(tmp_path: Path, *, private: bool = False) -> tuple[Path, Path]:
    """Return ``(result file, case file)``: two findings, the first labelled true."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (tmp_path / "fixture").mkdir()
    (tmp_path / "p.patch").write_text("", encoding="utf-8")
    first, second = _finding("sentinel", 3, "verified"), _finding("architect", 40, "refuted")
    case_file = corpus / "c1.yaml"
    case_file.write_text(
        yaml.safe_dump(
            {
                "case": "c1",
                "private": private,
                "source": {"kind": "local", "fixture": "../fixture", "patch": "../p.patch"},
                "planted": [
                    {"file": "src/a.py", "lines": [2, 4], "kind": "security", "severity": "blocker"}
                ],
                "findings": [
                    {
                        "fingerprint": first.fingerprint,
                        "label": True,
                        "labeller": "owner",
                        "labelled_on": "2026-10-21",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    result = BenchResult(
        corpus="corpus",
        corpus_root=str(corpus),
        corpus_sha="abcdef012345",
        provider="claude_code",
        reviewers=["architect", "sentinel", "tester", "scribe"],
        measured_on=date(2026, 10, 21),
        started_at=datetime(2026, 10, 21, tzinfo=UTC),
        cases=[CaseRun(case="c1", kind="local", findings=[first, second], tokens=1200, wall_s=61)],
    )
    return save_result(result, tmp_path / "results"), case_file


def test_report_prints_both_tables(tmp_path: Path) -> None:
    result_file, _ = recorded(tmp_path)
    out = CliRunner().invoke(app, ["bench", "report", str(result_file)])
    assert out.exit_code == 0, out.output
    lines = out.stdout.splitlines()
    assert "reviewer | TP | FP | unsure | precision | refuter precision | recall" in lines
    assert "severity | TP | FP | unsure | precision | refuter precision | recall" in lines
    assert "sentinel | 1 | 0 | 0 | 1.00 (1/1) | cannot say | 1.00 (1/1)" in lines
    assert "c1 | 2 | 1 | 1 | 1 | 1200 | 61" in lines


def test_score_is_the_same_command(tmp_path: Path) -> None:
    result_file, _ = recorded(tmp_path)
    runner = CliRunner()
    report = runner.invoke(app, ["bench", "report", str(result_file)]).stdout
    assert runner.invoke(app, ["bench", "score", str(result_file)]).stdout == report


def test_readme_line_refused_below_one_hundred(tmp_path: Path) -> None:
    result_file, _ = recorded(tmp_path)
    out = CliRunner().invoke(app, ["bench", "report", str(result_file), "--readme-line"])
    assert out.exit_code == 2
    assert "corpus too small: 1 < 100" in out.stderr


def test_update_readme_replaces_only_the_block(tmp_path: Path) -> None:
    result_file, _ = recorded(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(
        "# x\n<!-- bench:precision:start -->\nold\n<!-- bench:precision:end -->\ntail\n",
        encoding="utf-8",
    )
    out = CliRunner().invoke(
        app, ["bench", "report", str(result_file), "--update-readme", str(readme)]
    )
    assert out.exit_code == 0, out.output
    text = readme.read_text(encoding="utf-8")
    assert "old" not in text
    assert text.startswith("# x\n<!-- bench:precision:start -->\ncorpus corpus@abcdef012345")
    assert "| reviewer | TP | FP | unsure | precision | refuter precision | recall |" in text
    assert text.endswith("<!-- bench:precision:end -->\ntail\n")


def test_update_readme_without_the_block_exits_two(tmp_path: Path) -> None:
    result_file, _ = recorded(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text("# x\n", encoding="utf-8")
    out = CliRunner().invoke(
        app, ["bench", "report", str(result_file), "--update-readme", str(readme)]
    )
    assert out.exit_code == 2


def test_label_writes_one_stub_per_unlabelled_finding(tmp_path: Path) -> None:
    result_file, case_file = recorded(tmp_path)
    out = CliRunner().invoke(app, ["bench", "label", str(result_file)])
    assert out.exit_code == 0, out.output
    case = load_case(case_file)
    assert list(case.labels().values()) == ["true", None]
    assert "architect blocker src/a.py:40: architect claim at 40" in case.findings[1].hint
    again = CliRunner().invoke(app, ["bench", "label", str(result_file)])
    assert "0 stubs written" in again.stderr


def test_label_interactive_records_the_answer(tmp_path: Path) -> None:
    result_file, case_file = recorded(tmp_path)
    out = CliRunner().invoke(
        app,
        ["bench", "label", str(result_file), "--interactive", "--labeller", "owner"],
        input="f\n",
    )
    assert out.exit_code == 0, out.output
    entry = load_case(case_file).findings[1]
    assert (entry.label, entry.labeller) == ("false", "owner")
    assert entry.labelled_on == date.today()


def test_private_case_stub_has_no_hint(tmp_path: Path) -> None:
    result_file, case_file = recorded(tmp_path, private=True)
    CliRunner().invoke(app, ["bench", "label", str(result_file)])
    assert "claim at 40" not in case_file.read_text(encoding="utf-8")


def test_report_on_a_missing_result_exits_two(tmp_path: Path) -> None:
    out = CliRunner().invoke(app, ["bench", "report", str(tmp_path / "none.json")])
    assert out.exit_code == 2
