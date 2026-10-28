"""SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ AC2-AC10, AC13-AC15: the gate end to end.

A real git origin carries the base (``main``) and each PR head; the
forge serves the PR's three-dot diff; the four reviewers approve without
a comment and record what they were shown. ``data/vectors.json`` is a
two-line artefact whose ``vectors`` line is 100 000 characters, so a
regeneration's diff unit (``-`` and ``+``) is about 200 000 characters,
above the reviewers' 140 000 cap. ``scripts/check_vectors.py`` is the
declared check: it exits 0 when the artefact's key names the sha256 of
``data/corpus.json``, else prints ``Embeddings are stale: key names a
previous corpus`` and exits 1. ``review generated`` runs it for real
through a recording runner; ``review pr`` reads its evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
import typer
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs
from typer.testing import CliRunner

from repoach.cli import app, review_cmds
from repoach.core.logging import get_logger
from repoach.review import generated_process, generated_verify
from repoach.review.finding_verifiers import verify_finding
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from repoach.review.findings_bridge import oversized_finding
from repoach.review.generated_artefacts import run_checks
from repoach.review.generated_evidence import evidence_payload
from repoach.review.generated_git import blob_at, changed_paths, merge_base, resolve_commit, run_git
from repoach.review.generated_process import RunResult, subprocess_runner
from repoach.review.generated_settle import settle_at_head, settlements_at_head
from repoach.review.generated_store import (
    fetch_generated_checks,
    has_generated_checks,
    record_generated_checks,
)
from repoach.review.generated_types import (
    GeneratedCheck,
    GeneratedEntry,
    GeneratedState,
    GeneratedTiming,
)
from repoach.review.generated_verify import head_tree
from repoach.review.merge_gate import MergeDecision, MergeFacts
from repoach.review.report import fenced, render_ledger_report
from repoach.review.secret_env import scrubbed_env
from tests.fixtures.generated_artefacts.harness import (
    Bench,
    RecordingRunner,
    Repo,
    git,
    install,
    review,
    verify,
)

ARTEFACT = "data/vectors.json"
CHECK = "scripts/check_vectors.py"
STALE = "Embeddings are stale: key names a previous corpus"

CHECK_SCRIPT = (
    '"""Check that data/vectors.json is keyed to data/corpus.json."""\n'
    "import hashlib\n"
    "import json\n"
    "import pathlib\n"
    "import sys\n"
    "\n"
    'corpus = pathlib.Path("data/corpus.json").read_bytes()\n'
    'key = json.loads(pathlib.Path("data/vectors.json").read_text())["corpus_sha256"]\n'
    "if key != hashlib.sha256(corpus).hexdigest():\n"
    f'    print("{STALE}")\n'
    "    sys.exit(1)\n"
    'print("data/vectors.json matches data/corpus.json")\n'
)


def _toml(*entries: str, languages: str = '["python"]') -> str:
    return f"[project]\nlanguages = {languages}\n" + "".join(entries)


def _entry(
    path: str = ARTEFACT,
    sources: str = '["data/corpus.json"]',
    producer_inputs: str | None = None,
) -> str:
    text = (
        "\n[[review.generated]]\n"
        f'path = "{path}"\n'
        f'check = ["{CHECK}", "--check"]\n'
        f"sources = {sources}\n"
    )
    if producer_inputs is not None:
        text += f"producer_inputs = {producer_inputs}\n"
    return text


def _vectors(corpus: str, fill: str) -> str:
    key = hashlib.sha256(corpus.encode()).hexdigest()
    return f'{{"corpus_sha256": "{key}",\n "vectors": "{fill * 100_000}"}}\n'


CORPUS_V1 = '{"chunks": ["one"]}\n'
CORPUS_V2 = '{"chunks": ["one", "two"]}\n'


def _base(toml: str, **extra: str) -> dict[str, str]:
    files = {
        "repoach.toml": toml,
        "README.md": "demo\n",
        "data/corpus.json": CORPUS_V1,
        ARTEFACT: _vectors(CORPUS_V1, "A"),
        CHECK: CHECK_SCRIPT,
    }
    files.update(extra)
    return files


@pytest.fixture
def bench(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Bench:
    """The stubbed reviewers, judge and ledger of one test."""
    return install(monkeypatch, tmp_path)


def _evidence(
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    head: str,
    *,
    pr: int = 7,
) -> tuple[Path, dict[str, object], list[str], RecordingRunner]:
    out = repo.checkout.parent / f"review-generated-{head[:7]}.json"
    payload, err, runner = verify(repo, monkeypatch, capsys, head=head, out=out, pr=pr)
    return out, payload, err, runner


def _generated_findings(db_path: Path, pr: int = 7) -> list:
    return [
        f
        for f in fetch_findings(db_path, pr)
        if f.claim_type is ClaimType.GENERATED_ARTEFACT and f.file == ARTEFACT
    ]


def _oversized(db_path: Path, pr: int = 7) -> list:
    return [f for f in fetch_findings(db_path, pr) if f.finder == "diff_scoper"]


def test_declaration_read_from_base_ref(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC2: an entry the head adds does not apply; after it lands, the next PR uses it."""
    repo = Repo(tmp_path, _base(_toml()))
    head = repo.push_head(
        {
            "repoach.toml": _toml(_entry()),
            "data/corpus.json": CORPUS_V2,
            ARTEFACT: _vectors(CORPUS_V2, "B"),
        }
    )
    evidence, _, _, runner = _evidence(repo, monkeypatch, capsys, head)
    assert runner.calls == []
    first = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    assert first.payload["final_verdict"] == "REQUEST_CHANGES"
    assert first.payload["generated"] == []
    assert [f.file for f in _oversized(bench.db_path)] == [ARTEFACT]

    repo.merge(7)
    corpus_v3 = '{"chunks": ["one", "two", "three"]}\n'
    next_head = repo.push_head(
        {"data/corpus.json": corpus_v3, ARTEFACT: _vectors(corpus_v3, "C")}, pr=8
    )
    evidence, _, _, runner = _evidence(repo, monkeypatch, capsys, next_head, pr=8)
    second = review(repo, monkeypatch, capsys, head=next_head, pr=8, evidence=evidence)

    assert len(runner.calls) == 1
    assert second.payload["final_verdict"] == "APPROVE"
    assert _oversized(bench.db_path, pr=8) == []


def test_declared_check_passes_merges(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC3: the reviewers see the paragraph, the row is verified, nothing blocks."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B")})
    assert len(repo.diffs[7]) > 200_000
    evidence, _, _, _ = _evidence(repo, monkeypatch, capsys, head)

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    assert bench.prompts.diffs
    vectors_line = "B" * 100_000
    for diff in bench.prompts.diffs:
        assert vectors_line not in diff
        assert "A" * 100_000 not in diff
        assert f"generated artefact {ARTEFACT}: " in diff
        assert "sources changed in this diff: data/corpus.json" in diff
        assert f"proved by: {CHECK} --check" in diff
    checks, _ = fetch_generated_checks(bench.db_path, 7, head)
    assert [(c.path, c.state) for c in checks] == [(ARTEFACT, GeneratedState.VERIFIED)]
    assert [f for f in fetch_findings(bench.db_path, 7) if f.file == ARTEFACT] == []
    assert result.payload["final_verdict"] == "APPROVE"
    assert result.exit_code == 0


def test_declared_check_fails_is_refused_with_reason(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC4: a failing check is one blocking finding naming the check's last line."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V1, "B")})
    evidence, _, _, _ = _evidence(repo, monkeypatch, capsys, head)

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    findings = _generated_findings(bench.db_path)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.claim == f"generated artefact not derived from its sources: {ARTEFACT}"
    assert finding.severity.value == "blocking"
    assert finding.status is FindingStatus.VERIFIED
    assert "check exited 1: Embeddings are stale" in finding.verification_result
    assert result.payload["final_verdict"] == "REQUEST_CHANGES"
    assert result.exit_code == 2


def test_declared_sources_untouched_is_refused(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC5: the artefact changed, no source did, the check passes: still refused."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({ARTEFACT: _vectors(CORPUS_V1, "B")})
    evidence, payload, _, _ = _evidence(repo, monkeypatch, capsys, head)
    assert payload["checks"][0]["exit_code"] == 0

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    findings = _generated_findings(bench.db_path)
    assert [f.verification_result for f in findings] == ["no source changed"]
    assert result.payload["final_verdict"] == "REQUEST_CHANGES"


def test_undeclared_oversized_names_the_remedy(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC6: an undeclared oversized file keeps its finding, which names the remedy."""
    repo = Repo(tmp_path, _base(_toml()))
    head = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B")})

    result = review(repo, monkeypatch, capsys, head=head)

    oversized = _oversized(bench.db_path)
    assert len(oversized) == 1
    finding = oversized[0]
    assert finding.file == ARTEFACT
    assert finding.claim_type is ClaimType.SPEC_GAP
    assert finding.severity.value == "blocking"
    assert finding.status is FindingStatus.VERIFIED
    assert "declare it under [[review.generated]] with its check, or split it" in finding.claim
    assert result.payload["final_verdict"] == "REQUEST_CHANGES"


class _SpawnLog:
    """Every argv ``subprocess.Popen`` was given."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []


def test_check_never_runs_in_review_job(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC7: ``review pr`` spawns no check; ``review generated`` runs it shell-free and scrubbed."""
    monkeypatch.setenv("REPOACH_GITLAB_TOKEN", "glpat-secret")
    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", "sk-secret")
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B")})
    evidence, _, err, verifier = _evidence(repo, monkeypatch, capsys, head)
    (notice,) = [line for line in err if line.startswith("repoach: review generated re-runs")]
    assert "REPOACH_ANTHROPIC_AUTH_TOKEN" in notice
    assert "REPOACH_GITLAB_TOKEN" in notice
    ((executable, argv, env),) = verifier.reexecs
    assert argv[:6] == [executable, "-P", "-m", "repoach", "review", "generated"]
    assert "REPOACH_GITLAB_TOKEN" not in env
    assert "REPOACH_ANTHROPIC_AUTH_TOKEN" not in env

    assert len(verifier.calls) == 1
    call = verifier.calls[0]
    assert call.shell is False
    assert CHECK in call.argv
    assert call.env == scrubbed_env()
    assert "REPOACH_GITLAB_TOKEN" not in call.env
    assert "REPOACH_ANTHROPIC_AUTH_TOKEN" not in call.env

    in_review = RecordingRunner()
    monkeypatch.setattr(generated_verify.SEAMS, "runner", in_review)
    spawned = _SpawnLog()
    real_popen = subprocess.Popen

    class _LoggingPopen(real_popen):
        def __init__(self, args: object, *rest: object, **kwargs: object) -> None:
            spawned.argvs.append([str(a) for a in args] if isinstance(args, list) else [str(args)])
            super().__init__(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _LoggingPopen)
    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    assert in_review.calls == []
    assert spawned.argvs
    assert not any(CHECK in " ".join(argv) for argv in spawned.argvs)
    assert result.payload["final_verdict"] == "APPROVE"


def test_check_script_absent_at_head_is_refused(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC8 (absent): the check script deleted by the diff is never green."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head(
        {"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B"), CHECK: None}
    )
    evidence, payload, _, runner = _evidence(repo, monkeypatch, capsys, head)
    assert runner.calls == []
    assert payload["checks"][0]["state"] == GeneratedState.CHECK_ABSENT.value

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    findings = _generated_findings(bench.db_path)
    assert [f.verification_result for f in findings] == [
        f"check script {CHECK} absent at {head[:7]}"
    ]
    assert result.payload["final_verdict"] == "REQUEST_CHANGES"


def test_check_over_timeout_is_skipped_for_cost(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC8 (cost): a check outliving ``check_timeout_s`` on a fake clock is skipped, never green."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B")})
    now = [0.0]

    def clock() -> float:
        return now[0]

    def slow(argv: object, **_kwargs: object) -> RunResult:
        now[0] += 121.0
        return RunResult(0, "too late")

    entry = GeneratedEntry(path=ARTEFACT, check=(CHECK, "--check"), sources=("data/corpus.json",))
    git_seed = repo.seed
    subprocess.run(["git", "checkout", "-q", head], cwd=git_seed, check=True)
    checks = run_checks(
        {ARTEFACT: entry},
        head_dir=git_seed,
        head_sha=head,
        runner=slow,
        changed_files=["data/corpus.json", ARTEFACT],
        clock=clock,
    )
    assert [(c.state, c.exit_code) for c in checks] == [(GeneratedState.SKIPPED_COST, None)]
    assert checks[0].output_tail == f"check not run within 120 s: {ARTEFACT}"
    evidence = tmp_path / "cost.json"
    evidence.write_text(
        json.dumps(
            evidence_payload(
                pr_number=7,
                head_sha=head,
                base_sha=git(repo.seed, "merge-base", "main", head),
                checks=checks,
                timing=GeneratedTiming(121_000, 5),
            )
        ),
        encoding="utf-8",
    )

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    findings = _generated_findings(bench.db_path)
    assert [f.verification_result for f in findings] == [f"check not run within 120 s: {ARTEFACT}"]
    assert result.payload["final_verdict"] == "REQUEST_CHANGES"


def test_evidence_of_another_head_is_not_run(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC8 (not_run): evidence of an earlier head is refused and the artefact reported not run."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    first = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B")})
    evidence, _, _, _ = _evidence(repo, monkeypatch, capsys, first)
    second = repo.push_head({"README.md": "demo, again\n"})

    result = review(repo, monkeypatch, capsys, head=second, evidence=evidence)

    findings = _generated_findings(bench.db_path)
    assert [f.verification_result for f in findings] == [
        f"no check evidence for {ARTEFACT} at {second[:7]}"
    ]
    checks, _ = fetch_generated_checks(bench.db_path, 7, second)
    assert [c.state for c in checks] == [GeneratedState.NOT_RUN]
    assert result.payload["final_verdict"] == "REQUEST_CHANGES"


def test_declared_but_untouched_runs_nothing(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC8 (untouched): a declared artefact the diff does not change runs and records nothing."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({"README.md": "demo, again\n"})
    evidence, payload, err, runner = _evidence(repo, monkeypatch, capsys, head)

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    assert runner.calls == []
    assert payload["checks"] == []
    assert "generated: no declared generated artefact touched" in err
    assert fetch_generated_checks(bench.db_path, 7, head) == ([], None)
    assert result.payload["generated"] == []
    assert not any(line.startswith("generated:") for line in result.stderr)
    assert result.payload["final_verdict"] == "APPROVE"


def test_two_artefacts_two_rows_in_declaration_order(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC8 (two_artefacts): two touched artefacts are two rows, in declaration order."""
    index = "data/index.json"
    toml = _toml(_entry(), _entry(path=index))
    repo = Repo(tmp_path, _base(toml, **{index: '{"n": 1}\n'}))
    head = repo.push_head(
        {"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B"), index: '{"n": 2}\n'}
    )
    evidence, _, _, runner = _evidence(repo, monkeypatch, capsys, head)

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    assert len(runner.calls) == 2
    checks, _ = fetch_generated_checks(bench.db_path, 7, head)
    assert [c.path for c in checks] == [ARTEFACT, index]
    assert [g["path"] for g in result.payload["generated"]] == [ARTEFACT, index]
    assert result.payload["final_verdict"] == "APPROVE"


_VERIFIED_LINE = re.compile(
    rf"^generated: {re.escape(ARTEFACT)} verified by {re.escape(CHECK)} --check \((\d+) ms\)$"
)


def test_report_names_the_proving_command(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC9: the JSON ``generated[0]`` and the line saying what proved the artefact."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B")})
    evidence, _, _, _ = _evidence(repo, monkeypatch, capsys, head)

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    entry = result.payload["generated"][0]
    assert entry["path"] == ARTEFACT
    assert entry["check"] == f"{CHECK} --check"
    assert entry["state"] == "generated_artefact_verified"
    assert isinstance(entry["ms"], int)
    assert entry["sources_changed"] == ["data/corpus.json"]
    matches = [_VERIFIED_LINE.match(line) for line in result.stderr]
    assert [m.group(1) for m in matches if m] == [str(entry["ms"])]


def test_older_oversized_findings_settle_without_refuter(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC10: two older-head oversized findings on a now-declared path settle from the row."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B")})
    init_findings_schema(bench.db_path)
    older = ["0cc9008955602f88b24162f93faafa8ca71741c3", "7cd8971d9402fe4fe92f380b8ece07f4704ab68c"]
    for round_n, sha in enumerate(older, start=1):
        record_finding(
            bench.db_path, oversized_finding(ARTEFACT, pr_number=7, head_sha=sha, round_n=round_n)
        )
    evidence, _, _, _ = _evidence(repo, monkeypatch, capsys, head)

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    settled = [f for f in fetch_findings(bench.db_path, 7) if f.head_sha in older]
    assert len(settled) == 2
    for finding in settled:
        assert finding.status is FindingStatus.RESOLVED_AT_HEAD
        assert f"declared generated at {head[:7]}, verified by" in finding.verification_result
    assert bench.judge.calls == 0
    assert result.payload["final_verdict"] == "APPROVE"


def test_producer_input_counts_as_source(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC13: a changed producer input is a source; without the declaration, no source changed."""
    header = "data/_header.json"
    changes = {header: '{"model": "v2"}\n', ARTEFACT: _vectors(CORPUS_V1, "B")}
    declared = Repo(
        tmp_path / "declared",
        _base(_toml(_entry(producer_inputs=f'["{header}"]')), **{header: '{"model": "v1"}\n'}),
    )
    head = declared.push_head(changes)
    evidence, _, _, _ = _evidence(declared, monkeypatch, capsys, head)

    result = review(declared, monkeypatch, capsys, head=head, evidence=evidence)

    assert result.payload["final_verdict"] == "APPROVE"
    assert _generated_findings(bench.db_path) == []
    assert f"producer inputs changed: {header}" in result.stderr
    assert result.payload["generated"][0]["producer_inputs_changed"] == [header]

    bare = install(monkeypatch, tmp_path / "bare")
    plain = Repo(tmp_path / "bare", _base(_toml(_entry()), **{header: '{"model": "v1"}\n'}))
    head = plain.push_head(changes)
    evidence, _, _, _ = _evidence(plain, monkeypatch, capsys, head)

    refused = review(plain, monkeypatch, capsys, head=head, evidence=evidence)

    assert [f.verification_result for f in _generated_findings(bare.db_path)] == [
        "no source changed"
    ]
    assert refused.payload["final_verdict"] == "REQUEST_CHANGES"


DIGEST_SCRIPT = CHECK_SCRIPT.replace('["corpus_sha256"]', '["corpus_digest"]')


def _digest_vectors(corpus: str, fill: str) -> str:
    return _vectors(corpus, fill).replace('"corpus_sha256"', '"corpus_digest"')


def test_changed_check_script_runs_base_and_head(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC14: a changed check runs twice, base script first; either failing refuses."""
    repo = Repo(tmp_path / "renamed", _base(_toml(_entry())))
    head = repo.push_head(
        {
            "data/corpus.json": CORPUS_V2,
            ARTEFACT: _digest_vectors(CORPUS_V2, "B"),
            CHECK: DIGEST_SCRIPT,
        }
    )
    evidence, _, _, runner = _evidence(repo, monkeypatch, capsys, head)

    assert len(runner.calls) == 2
    assert runner.calls[0].script == CHECK_SCRIPT
    assert runner.calls[1].script == DIGEST_SCRIPT
    refused = review(repo, monkeypatch, capsys, head=head, evidence=evidence)
    results = [f.verification_result for f in _generated_findings(bench.db_path)]
    assert len(results) == 1
    assert results[0].startswith("base check exited 1")
    assert refused.payload["final_verdict"] == "REQUEST_CHANGES"

    both = install(monkeypatch, tmp_path / "both")
    cosmetic = CHECK_SCRIPT + 'print("checked")\n'
    repo = Repo(tmp_path / "both", _base(_toml(_entry())))
    head = repo.push_head(
        {"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B"), CHECK: cosmetic}
    )
    evidence, _, _, runner = _evidence(repo, monkeypatch, capsys, head)

    assert [call.script for call in runner.calls] == [CHECK_SCRIPT, cosmetic]
    passed = review(repo, monkeypatch, capsys, head=head, evidence=evidence)
    assert passed.payload["final_verdict"] == "APPROVE"
    assert _generated_findings(both.db_path) == []
    lines = [line for line in passed.stderr if line.startswith("generated: ")]
    assert len(lines) == 2
    assert "(base script, " in lines[0]
    assert "(head script, " in lines[1]


def test_delay_is_measured_and_printed(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC15: the evidence carries integer checks_ms and overhead_ms; the report prints them."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B")})
    evidence, payload, err, _ = _evidence(repo, monkeypatch, capsys, head)

    assert isinstance(payload["checks_ms"], int)
    assert isinstance(payload["overhead_ms"], int)
    expected = f"generated checks: {payload['checks_ms']} ms + {payload['overhead_ms']} ms overhead"
    assert expected in err
    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)
    assert expected in result.stderr


def test_evidence_states_are_recomputed_at_review(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The evidence cannot claim a source the diff lacks, nor verify a non-zero exit."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({ARTEFACT: _vectors(CORPUS_V1, "B")})
    evidence, payload, _, _ = _evidence(repo, monkeypatch, capsys, head)
    forged = payload["checks"][0]
    forged.update(state="generated_artefact_verified", sources_changed=["data/corpus.json"])
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    assert [f.verification_result for f in _generated_findings(bench.db_path)] == [
        "no source changed"
    ]
    assert result.payload["generated"][0]["sources_changed"] == []
    assert result.payload["final_verdict"] == "REQUEST_CHANGES"

    other = install(monkeypatch, tmp_path / "exit")
    repo = Repo(tmp_path / "exit", _base(_toml(_entry())))
    head = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V1, "B")})
    evidence, payload, _, _ = _evidence(repo, monkeypatch, capsys, head)
    assert payload["checks"][0]["exit_code"] == 1
    payload["checks"][0]["state"] = "generated_artefact_verified"
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    refused = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    results = [f.verification_result for f in _generated_findings(other.db_path)]
    assert len(results) == 1
    assert results[0].startswith("check exited 1: Embeddings are stale")
    assert refused.payload["final_verdict"] == "REQUEST_CHANGES"


def test_check_output_is_made_inert_in_the_cause(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing check's last line reaches the finding without markup, controls or excess length."""
    hostile = "`@owner` <img src=x> [link](http://x) \x1b[31m" + "z" * 400
    script = f"import sys\nprint({hostile!r})\nsys.exit(3)\n"
    repo = Repo(tmp_path, _base(_toml(_entry()), **{CHECK: script}))
    head = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B")})
    evidence, _, _, _ = _evidence(repo, monkeypatch, capsys, head)

    review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    (result,) = [f.verification_result for f in _generated_findings(bench.db_path)]
    assert result.startswith("check exited 3: ")
    cause = result.removeprefix("check exited 3: ")
    assert len(cause) <= 200
    for char in "`@<>[]\x1b":
        assert char not in cause


def test_report_section_fences_check_output(tmp_path: Path) -> None:
    """The Generated artefacts section is a fence no backtick run in a line can close."""
    lines = ["generated: data/vectors.json generated_artefact_stale: check exited 1: ```` [x](y)"]
    init_findings_schema(tmp_path / "ledger.db")
    body = render_ledger_report(
        tmp_path / "ledger.db",
        pr_number=7,
        decision=MergeDecision(merge=True, reasons=[]),
        facts=MergeFacts(
            head_sha="a" * 40,
            ci_green=True,
            open_blocking_findings=0,
            spec_covered=True,
            spec_coverage_known=False,
            review_complete=True,
            review_integrity_known=True,
        ),
        generated_lines=lines,
    )
    section = body.split("## Generated artefacts\n\n", 1)[1].split("\n\n## Findings", 1)[0]
    assert section == "`````text\n" + lines[0] + "\n`````"


def test_symlinked_check_script_is_never_run(tmp_path: Path) -> None:
    """A check script that is a symlink is refused before any run, so D3 never writes through it."""
    head_dir = tmp_path / "head"
    (head_dir / "data").mkdir(parents=True)
    (head_dir / "scripts").mkdir()
    (head_dir / ARTEFACT).write_text("{}\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    (head_dir / CHECK).symlink_to(outside)
    entry = GeneratedEntry(path=ARTEFACT, check=(CHECK, "--check"), sources=("data/corpus.json",))
    runner = RecordingRunner()

    checks = run_checks(
        {ARTEFACT: entry},
        head_dir=head_dir,
        head_sha="f" * 40,
        runner=runner,
        changed_files=[ARTEFACT, "data/corpus.json", CHECK],
        base_blob=lambda _path: b"import sys\nsys.exit(0)\n",
    )

    assert runner.calls == []
    assert outside.read_text(encoding="utf-8") == "print('outside')\n"
    assert [(c.state, c.output_tail) for c in checks] == [
        (GeneratedState.STALE, f"check script {CHECK} is a symlink or leaves the head tree")
    ]


def test_no_process_of_the_check_outlives_its_run(tmp_path: Path) -> None:
    """A process the check leaves behind is killed, so it cannot rewrite the evidence later."""
    marker = tmp_path / "late-write"
    child = tmp_path / "late_child.py"
    child.write_text(
        f"import pathlib, time\ntime.sleep(1)\npathlib.Path({str(marker)!r}).write_text('x')\n",
        encoding="utf-8",
    )
    script = tmp_path / "leave_child.py"
    script.write_text(
        f"import subprocess, sys\nsubprocess.Popen([sys.executable, {str(child)!r}])\nprint('done')\n",
        encoding="utf-8",
    )

    result = subprocess_runner(
        ["python3", str(script)], cwd=tmp_path, env=scrubbed_env(), timeout_s=30, shell=False
    )

    assert (result.exit_code, result.output.strip()) == (0, "done")
    subprocess.run(["sleep", "2"], check=True)
    assert not marker.exists()


def test_head_tree_is_a_clone_with_its_own_git_dir(tmp_path: Path) -> None:
    """The check never runs in the checkout nor beside its .git: a clone is made and removed."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    sha = git(repo.checkout, "rev-parse", "HEAD")

    with head_tree(repo.checkout, sha, tmp_path / "state") as tree:
        assert tree != repo.checkout
        common = Path(git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        assert common == tree / ".git"
        assert git(tree, "rev-parse", "HEAD") == sha
        assert (tree / CHECK).is_file()
        assert not (tree / ".git" / "objects" / "info" / "alternates").exists()
        assert (tree.parent.stat().st_mode & 0o777) == 0o700
        (tree / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\n", encoding="utf-8")

    assert not tree.exists()
    assert not (repo.checkout / ".git" / "hooks" / "post-checkout").exists()
    assert git(repo.checkout, "worktree", "list").count("\n") == 0


def test_a_detached_descendant_of_the_check_is_killed(tmp_path: Path) -> None:
    """A grandchild that leaves the check's session (setsid, double fork) is killed too."""
    marker = tmp_path / "late-write"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        f"import pathlib, time\ntime.sleep(1)\npathlib.Path({str(marker)!r}).write_text('x')\n",
        encoding="utf-8",
    )
    middle = tmp_path / "middle.py"
    middle.write_text(
        f"import subprocess, sys\nsubprocess.Popen([sys.executable, {str(grandchild)!r}], start_new_session=True)\n",
        encoding="utf-8",
    )
    script = tmp_path / "check.py"
    script.write_text(
        f"import subprocess, sys\nsubprocess.run([sys.executable, {str(middle)!r}])\nprint('done')\n",
        encoding="utf-8",
    )

    result = subprocess_runner(
        ["python3", str(script)], cwd=tmp_path, env=scrubbed_env(), timeout_s=30, shell=False
    )

    assert (result.exit_code, result.output.strip()) == (0, "done")
    subprocess.run(["sleep", "2"], check=True)
    assert not marker.exists()


def test_a_script_turned_into_a_symlink_by_the_base_run_is_not_written_through(
    tmp_path: Path,
) -> None:
    """D3: the base run turning the script into a symlink stops both runs, nothing written outside."""
    head_dir = tmp_path / "head"
    (head_dir / "data").mkdir(parents=True)
    (head_dir / "scripts").mkdir()
    (head_dir / ARTEFACT).write_text("{}\n", encoding="utf-8")
    (head_dir / CHECK).write_text("print('head')\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    entry = GeneratedEntry(path=ARTEFACT, check=(CHECK, "--check"), sources=("data/corpus.json",))

    def swapping_runner(argv: object, *, cwd: Path, **_kwargs: object) -> RunResult:
        script = cwd / CHECK
        script.unlink()
        script.symlink_to(outside)
        return RunResult(0, "base ok")

    checks = run_checks(
        {ARTEFACT: entry},
        head_dir=head_dir,
        head_sha="f" * 40,
        runner=swapping_runner,
        changed_files=[ARTEFACT, "data/corpus.json", CHECK],
        base_blob=lambda _path: b"print('base')\n",
    )

    assert outside.read_text(encoding="utf-8") == "print('outside')\n"
    assert [(c.script_ref, c.state, c.output_tail) for c in checks] == [
        (
            "base",
            GeneratedState.STALE,
            f"check script {CHECK} changed into a symlink during the run",
        ),
        (
            "head",
            GeneratedState.STALE,
            f"check script {CHECK} is a symlink or leaves the head tree",
        ),
    ]


def test_review_generated_requires_the_base() -> None:
    """The base is never read from the pull request's own repoach.toml: --base is required."""
    result = CliRunner().invoke(
        app,
        ["review", "generated", "--pr", "1", "--head", "x", "--out", "y"],
        env={
            "NO_COLOR": "1",
            "TERM": "dumb",
            "COLUMNS": "200",
            "FORCE_COLOR": "",
            "GITHUB_ACTIONS": "",
        },
    )

    assert result.exit_code == 2
    assert "--base" in result.output


def test_a_symlinked_artefact_is_never_read_and_never_green(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The review job, which holds secrets, reads no host file a symlinked artefact points at."""
    host = tmp_path / "host-token"
    host.write_text("s3cr3t-token-value\n", encoding="utf-8")
    repo = Repo(tmp_path / "repo", _base(_toml(_entry())))
    seed_artefact = repo.seed / ARTEFACT
    repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: None})
    seed_artefact.parent.mkdir(parents=True, exist_ok=True)
    git(repo.seed, "checkout", "-q", "pr-7")
    seed_artefact.symlink_to(host)
    git(repo.seed, "add", "-A")
    git(repo.seed, "commit", "-q", "-m", "link")
    git(repo.seed, "push", "-q", "-f", "origin", "HEAD:refs/pull/7/head")
    head = git(repo.seed, "rev-parse", "HEAD")
    repo.diffs[7] = git(repo.seed, "diff", "main...HEAD") + "\n"
    git(repo.seed, "checkout", "-q", "main")
    evidence, payload, _, runner = _evidence(repo, monkeypatch, capsys, head)

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    assert runner.calls == []
    assert (
        payload["checks"][0]["output_tail"] == f"artefact {ARTEFACT} is a symlink; it is not read"
    )
    secret_size = str(len(host.read_bytes()))
    for diff in bench.prompts.diffs:
        assert f"{ARTEFACT}: " in diff
        assert "a symlink, not read" in diff
        assert f"-> {secret_size} bytes" not in diff
    findings = _generated_findings(bench.db_path)
    assert [f.verification_result for f in findings] == [
        f"check failed: artefact {ARTEFACT} is a symlink; it is not read"
    ]
    assert result.payload["final_verdict"] == "REQUEST_CHANGES"


def test_subprocess_runner_refuses_a_shell(tmp_path: Path) -> None:
    """``shell=True`` is refused before anything starts."""
    with pytest.raises(ValueError, match="never runs through a shell"):
        subprocess_runner(["true"], cwd=tmp_path, env=scrubbed_env(), timeout_s=5, shell=True)


def test_a_timed_out_check_has_its_session_killed(tmp_path: Path) -> None:
    """A check past its timeout raises TimeoutExpired, and its session is gone."""
    marker = tmp_path / "late-write"
    script = tmp_path / "slow.py"
    script.write_text(
        f"import pathlib, time\ntime.sleep(2)\npathlib.Path({str(marker)!r}).write_text('x')\n",
        encoding="utf-8",
    )

    with pytest.raises(subprocess.TimeoutExpired):
        subprocess_runner(
            ["python3", str(script)], cwd=tmp_path, env=scrubbed_env(), timeout_s=0.5, shell=False
        )

    subprocess.run(["sleep", "3"], check=True)
    assert not marker.exists()


def test_a_forged_evidence_tail_is_bounded_and_inert(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The review re-trims a tail read from the evidence: 20 lines, 1000 characters, no controls."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V1, "B")})
    evidence, payload, _, _ = _evidence(repo, monkeypatch, capsys, head)
    payload["checks"][0]["output_tail"] = "\n".join(
        f"line {n} \x07" + "z" * 5000 for n in range(500)
    )
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    checks, _ = fetch_generated_checks(bench.db_path, 7, head)
    lines = checks[0].output_tail.splitlines()
    assert len(lines) == 20
    assert lines[-1].startswith("line 499 ")
    assert all(len(line) <= 1000 and "\x07" not in line for line in lines)


def test_only_the_end_of_a_long_output_is_read(tmp_path: Path) -> None:
    """A check printing megabytes is read back from its last 64 KiB only."""
    script = tmp_path / "loud.py"
    script.write_text("print('x' * 2_000_000)\nprint('the end')\n", encoding="utf-8")

    result = subprocess_runner(
        ["python3", str(script)], cwd=tmp_path, env=scrubbed_env(), timeout_s=30, shell=False
    )

    assert result.exit_code == 0
    assert len(result.output) <= 64 * 1024
    assert result.output.strip().endswith("the end")


def test_without_a_subreaper_the_session_is_still_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where no subreaper can be set, a child left in the check's session still dies."""
    monkeypatch.setattr(generated_process, "_adopt_orphans", lambda: False)
    marker = tmp_path / "late-write"
    child = tmp_path / "late_child.py"
    child.write_text(
        f"import pathlib, time\ntime.sleep(1)\npathlib.Path({str(marker)!r}).write_text('x')\n",
        encoding="utf-8",
    )
    script = tmp_path / "leave_child.py"
    script.write_text(
        f"import subprocess, sys\nsubprocess.Popen([sys.executable, {str(child)!r}])\nprint('done')\n",
        encoding="utf-8",
    )

    result = subprocess_runner(
        ["python3", str(script)], cwd=tmp_path, env=scrubbed_env(), timeout_s=30, shell=False
    )

    assert result.exit_code == 0
    subprocess.run(["sleep", "2"], check=True)
    assert not marker.exists()


def test_settle_at_head_leaves_every_other_finding_to_the_refuter() -> None:
    """Only generated and oversized findings on a recorded path settle from the rows."""
    settlements = {ARTEFACT: ("resolved", "generated_check", "declared generated at abc1234")}

    def finding(finder: str, claim_type: ClaimType, path: str = ARTEFACT) -> Finding:
        return Finding(
            pr_number=7,
            head_sha="a" * 40,
            round=1,
            finder=finder,
            claim_type=claim_type,
            severity=Severity.BLOCKING,
            file=path,
            line_start=0,
            line_end=0,
            claim="c",
            evidence_pointer="e",
        )

    assert settle_at_head(finding("diff_scoper", ClaimType.SPEC_GAP), settlements) is not None
    assert settle_at_head(finding("generated_check", ClaimType.GENERATED_ARTEFACT), settlements)
    assert settle_at_head(finding("architect", ClaimType.SPEC_GAP), settlements) is None
    assert settle_at_head(finding("diff_scoper", ClaimType.SECURITY), settlements) is None
    assert (
        settle_at_head(finding("diff_scoper", ClaimType.SPEC_GAP, "other.json"), settlements)
        is None
    )


def test_a_re_check_without_generated_rows_creates_no_table(tmp_path: Path) -> None:
    """A ledger that never recorded a generated check is read for nothing and left as it was."""
    db_path = tmp_path / "ledger.db"
    init_findings_schema(db_path)

    assert settlements_at_head(db_path, 7, "a" * 40) == {}
    assert not has_generated_checks(db_path)


def test_each_run_gets_its_own_tree_and_may_not_change_the_artefact(tmp_path: Path) -> None:
    """A run that rewrites the tree leaves the next run untouched, and changing the artefact is stale."""
    template = tmp_path / "template"
    (template / "data").mkdir(parents=True)
    (template / "scripts").mkdir()
    (template / ARTEFACT).write_text("{}\n", encoding="utf-8")
    (template / "data" / "index.json").write_text("{}\n", encoding="utf-8")
    (template / CHECK).write_text("print('head')\n", encoding="utf-8")
    copies: list[Path] = []

    @contextmanager
    def fresh_tree() -> Iterator[Path]:
        copy = tmp_path / f"run-{len(copies)}"
        shutil.copytree(template, copy)
        copies.append(copy)
        yield copy

    seen: list[str] = []

    def rewriting_runner(argv: object, *, cwd: Path, **_kwargs: object) -> RunResult:
        seen.append((cwd / ARTEFACT).read_text(encoding="utf-8"))
        (cwd / ARTEFACT).write_text('{"forged": true}\n', encoding="utf-8")
        return RunResult(0, "ok")

    entry = GeneratedEntry(path=ARTEFACT, check=(CHECK, "--check"), sources=("data/corpus.json",))
    index = GeneratedEntry(path="data/index.json", check=(CHECK,), sources=("data/corpus.json",))
    checks = run_checks(
        {ARTEFACT: entry, "data/index.json": index},
        head_dir=template,
        head_sha="f" * 40,
        runner=rewriting_runner,
        changed_files=[ARTEFACT, "data/index.json", "data/corpus.json"],
        fresh_tree=fresh_tree,
    )

    assert len(copies) == 2
    assert seen == ["{}\n", "{}\n"]
    assert (template / ARTEFACT).read_text(encoding="utf-8") == "{}\n"
    assert [(c.path, c.state, c.output_tail) for c in checks] == [
        (ARTEFACT, GeneratedState.STALE, f"the check changed {ARTEFACT}"),
        ("data/index.json", GeneratedState.VERIFIED, "ok"),
    ]


def test_evidence_missing_its_base_run_is_reported_not_run(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D3 at review time: a changed check script with no base run in the evidence is not green."""
    cosmetic = CHECK_SCRIPT + 'print("checked")\n'
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head(
        {"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B"), CHECK: cosmetic}
    )
    evidence, payload, _, _ = _evidence(repo, monkeypatch, capsys, head)
    payload["checks"] = [run for run in payload["checks"] if run["script_ref"] == "head"]
    payload["checks"][0]["runs_for_path"] = 1
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    checks, _ = fetch_generated_checks(bench.db_path, 7, head)
    assert [(c.script_ref, c.state) for c in checks] == [
        ("base", GeneratedState.NOT_RUN),
        ("head", GeneratedState.VERIFIED),
    ]
    assert [f.verification_result for f in _generated_findings(bench.db_path)] == [
        f"no base-script run for {ARTEFACT} at {head[:7]}"
    ]
    assert result.payload["final_verdict"] == "REQUEST_CHANGES"


def test_generated_checks_round_trip_keeps_script_ref_and_timing(tmp_path: Path) -> None:
    """Rows read back as written: base/head script refs, and no timing when none was given."""
    db_path = tmp_path / "ledger.db"
    run = GeneratedCheck(
        path=ARTEFACT,
        argv=(CHECK, "--check"),
        head_sha="a" * 40,
        artefact_sha256="b" * 64,
        sources_changed=("data/corpus.json",),
        state=GeneratedState.VERIFIED,
        exit_code=0,
        duration_ms=12,
        output_tail="ok",
        script_ref="base",
        runs_for_path=2,
    )
    head_run = replace(run, script_ref="head", duration_ms=15)

    record_generated_checks(db_path, pr_number=7, head_sha="a" * 40, checks=[run, head_run])
    assert fetch_generated_checks(db_path, 7, "a" * 40) == ([run, head_run], None)

    timing = GeneratedTiming(27, 300)
    record_generated_checks(db_path, pr_number=7, head_sha="a" * 40, checks=[run], timing=timing)
    assert fetch_generated_checks(db_path, 7, "a" * 40) == ([run], timing)


def test_evidence_of_another_base_is_not_run(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The review computes the base itself; evidence naming another base proved another diff."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({"data/corpus.json": CORPUS_V2, ARTEFACT: _vectors(CORPUS_V2, "B")})
    evidence, payload, _, _ = _evidence(repo, monkeypatch, capsys, head)
    payload["base_sha"] = "0" * 40
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    result = review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    checks, _ = fetch_generated_checks(bench.db_path, 7, head)
    assert [c.state for c in checks] == [GeneratedState.NOT_RUN]
    assert result.payload["final_verdict"] == "REQUEST_CHANGES"


def test_the_orphan_sweep_gives_up_with_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child the sweep can never reap ends the sweep after its bound, named in a warning."""
    ghost = 2**22 + 12345
    monkeypatch.setattr(generated_process, "_children", lambda: {ghost})
    monkeypatch.setattr(generated_process, "_log", get_logger(generated_process.__name__))

    with capture_logs() as logs:
        generated_process._kill_orphans(set())

    warnings = [entry for entry in logs if entry["event"] == "generated.orphans_left"]
    assert warnings == [
        {"event": "generated.orphans_left", "left": [ghost], "log_level": "warning"}
    ]


def test_without_proc_the_runner_degrades_to_the_session_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a platform without /proc and subreapers the check still runs, with a named warning."""
    monkeypatch.setattr(generated_process.sys, "platform", "darwin")
    monkeypatch.setattr(generated_process, "_log", get_logger(generated_process.__name__))
    script = tmp_path / "ok.py"
    script.write_text("print('fine')\n", encoding="utf-8")

    with capture_logs() as logs:
        result = subprocess_runner(
            ["python3", str(script)], cwd=tmp_path, env=scrubbed_env(), timeout_s=30, shell=False
        )

    assert (result.exit_code, result.output.strip()) == (0, "fine")
    assert not generated_process.orphan_sweep_available()
    assert [e["platform"] for e in logs if e["event"] == "generated.orphan_sweep_unavailable"] == [
        "darwin"
    ]


def test_review_generated_exits_2_on_invalid_base_declarations(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A base declaration the loader refuses (a glob reaching code) ends review generated with 2."""
    repo = Repo(tmp_path, _base(_toml(_entry(path="scripts/*"))))
    head = repo.push_head({"data/corpus.json": CORPUS_V2})
    monkeypatch.chdir(repo.checkout)
    for name in generated_verify.held_secrets():
        monkeypatch.delenv(name)
    git(repo.checkout, "fetch", "-q", "origin", "+refs/pull/7/head:refs/pull/7/head")
    capsys.readouterr()

    with pytest.raises(typer.Exit) as exited:
        review_cmds.review_generated(
            pr_number=7, head=head, out=str(tmp_path / "out.json"), base="main"
        )

    assert exited.value.exit_code == 2
    assert "review.generated[0].path" in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()


def test_git_reads_say_none_instead_of_guessing(tmp_path: Path) -> None:
    """An unknown ref, an unrelated history and an absent path answer None or nothing."""
    repo = Repo(tmp_path, _base(_toml(_entry())))
    head = repo.push_head({"data/corpus.json": CORPUS_V2})
    git(repo.seed, "checkout", "-q", "--orphan", "unrelated")
    git(repo.seed, "rm", "-q", "-rf", ".")
    (repo.seed / "other.txt").write_text("x\n", encoding="utf-8")
    git(repo.seed, "add", "-A")
    git(repo.seed, "commit", "-q", "-m", "unrelated")
    unrelated = git(repo.seed, "rev-parse", "HEAD")
    main = git(repo.seed, "rev-parse", "main")

    assert resolve_commit(repo.seed, "no-such-ref") is None
    assert resolve_commit(repo.seed, "main") == main
    assert merge_base(repo.seed, "main", unrelated) is None
    assert merge_base(repo.seed, "main", head) == main
    assert blob_at(repo.seed, "main", "no/such/file") is None
    assert blob_at(repo.seed, "main", "data/corpus.json") == CORPUS_V1.encode()
    assert changed_paths(repo.seed, main, head) == ["data/corpus.json"]
    assert run_git(repo.seed, "rev-parse", "--verify", "nope").returncode != 0


def test_a_file_new_under_a_wildcard_is_reviewed_in_full(
    tmp_path: Path,
    bench: Bench,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A path a wildcard declaration matches but the base never had is read, not summarised."""
    toml = _toml(_entry(path="data/*.vec"))
    repo = Repo(tmp_path, _base(toml, **{"data/a.vec": "old\n"}))
    head = repo.push_head(
        {"data/corpus.json": CORPUS_V2, "data/a.vec": "new\n", "data/b.vec": "smuggled line\n"}
    )
    evidence, payload, _, _ = _evidence(repo, monkeypatch, capsys, head)

    review(repo, monkeypatch, capsys, head=head, evidence=evidence)

    assert [c["path"] for c in payload["checks"]] == ["data/a.vec"]
    for diff in bench.prompts.diffs:
        assert "+smuggled line" in diff
        assert "generated artefact data/a.vec: " in diff
        assert "generated artefact data/b.vec" not in diff
    checks, _ = fetch_generated_checks(bench.db_path, 7, head)
    assert [c.path for c in checks] == ["data/a.vec"]


def test_fenced_uses_at_least_three_backticks() -> None:
    """Plain lines get a three-backtick fence; a longer run inside gets a longer one."""
    assert fenced(["plain", ""]) == "```text\nplain\n\n```"
    assert fenced(["a `b` c"]) == "```text\na `b` c\n```"
    assert fenced(["x ``` y"]) == "````text\nx ``` y\n````"


def test_a_generated_finding_stays_verified_at_its_own_head(tmp_path: Path) -> None:
    """The mechanical verifier never refutes or defers a generated_artefact finding on disk."""
    finding = Finding(
        pr_number=7,
        head_sha="a" * 40,
        round=1,
        finder="generated_check",
        claim_type=ClaimType.GENERATED_ARTEFACT,
        severity=Severity.BLOCKING,
        file=ARTEFACT,
        line_start=0,
        line_end=0,
        claim=f"generated artefact not derived from its sources: {ARTEFACT}",
        evidence_pointer="e",
        status=FindingStatus.VERIFIED,
        verification_result="check exited 1: stale",
    )

    assert verify_finding(finding, repo_root=tmp_path) == (
        FindingStatus.VERIFIED,
        "generated_check",
        "check exited 1: stale",
    )


@settings(max_examples=200, deadline=None)
@given(st.lists(st.text(alphabet="ab`", max_size=12), max_size=5))
def test_the_fence_is_longer_than_any_backtick_run(lines: list[str]) -> None:
    """Whatever the lines, the fence outruns every backtick run in them, and is at least three."""
    block = fenced(lines)
    fence = block.split("text", 1)[0]
    longest = max((len(run) for line in lines for run in re.findall("`+", line)), default=0)
    assert set(fence) == {"`"}
    assert len(fence) == max(3, longest + 1)
    assert block.endswith("\n" + fence)
