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
import subprocess
from pathlib import Path

import pytest

from repoach.cli import review_cmds
from repoach.review import generated_artefacts
from repoach.review.findings import (
    ClaimType,
    FindingStatus,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from repoach.review.findings_bridge import oversized_finding
from repoach.review.generated_artefacts import (
    GeneratedEntry,
    GeneratedState,
    GeneratedTiming,
    RunResult,
    evidence_payload,
    run_checks,
)
from repoach.review.generated_store import fetch_generated_checks
from repoach.review.generated_verify import head_tree
from repoach.review.merge_gate import MergeDecision, MergeFacts
from repoach.review.report import render_ledger_report
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
    assert argv[:5] == [executable, "-m", "repoach", "review", "generated"]
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
    monkeypatch.setattr(review_cmds, "GENERATED_RUNNER", in_review)
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
                base_sha="",
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

    result = generated_artefacts.subprocess_runner(
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

    result = generated_artefacts.subprocess_runner(
        ["python3", str(script)], cwd=tmp_path, env=scrubbed_env(), timeout_s=30, shell=False
    )

    assert (result.exit_code, result.output.strip()) == (0, "done")
    subprocess.run(["sleep", "2"], check=True)
    assert not marker.exists()
