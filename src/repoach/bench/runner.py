"""``repoach bench run``: review every case through the real pipeline and record its findings.

Each reviewed case gets its own throw-away ledger (a fresh SQLite file
in a temporary directory), so no earlier run of the same head and no
other case can leak findings into it, and the user's ledger is never
written. The usage rows still go to the usage ledger, as every review's
do; their in-memory copy gives the case's tokens.

Sources:

* ``local`` — the fixture tree is copied, committed as the base, the
  patch applied and committed as the head; :class:`LocalDiffForge`
  serves the pair as a pull request.
* ``forge`` — the PR / MR of the repository the command runs in, its
  head fetched and materialised as for ``review pr`` (a head that moved
  away from the case's ``head_sha`` skips the case).
* ``ledger`` — no review: the findings recorded for ``pr`` at
  ``head_sha`` in a ledger are read as they are (the private Yoob
  reviews, NG3).

The review is a dry run: :class:`BenchOrchestrator` never posts, and
does not fire the proposed-escalation routine either.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from ..core.config import GatewayConfigError, get_settings
from ..core.logging import get_logger
from ..forge.protocol import Forge, ForgeError
from ..forge.runner import spawn
from ..forge.select import select_forge
from ..project.resolve import infer_forge_from_url
from ..providers.errors import ProviderConfigError
from ..providers.select import require_provider_ready
from ..providers.usage import totals, usage_run
from ..review.findings import Finding, fetch_findings
from ..review.orchestrator import ReviewTeamOrchestrator, TeamOutcome, resolve_fresh_head
from ..review.review_head import materialise_head, require_head_sha
from ..review.reviewer import ReviewVerdict
from .corpus import UNRECORDED_HEAD, Case, Corpus, fingerprint
from .digest import pr_link, web_url
from .local_forge import LocalDiffForge
from .result import BenchResult, CaseRun, RecordedFinding
from .score import REVIEWERS

_log = get_logger(__name__)

_GIT_TIMEOUT_S = 60
_BENCH_IDENTITY = ["-c", "user.name=repoach bench", "-c", "user.email=bench@repoach.invalid"]

SeverityKey = tuple[str, str, int, str]
"""``(reviewer, file, line, claim)`` — how a ledger row finds its comment's severity."""


class BenchUnavailableError(RuntimeError):
    """The provider (or the forge a case needs) cannot be used; nothing was reviewed."""


@dataclass(frozen=True)
class ReviewRequest:
    """What one review needs.

    Attributes:
        forge: Serves the diff and the pull request.
        pr_number: The number the case is reviewed as.
        head_sha: The head reviewed.
        tree: The head tree every reader resolves against.
        db_path: The case's throw-away ledger.
    """

    forge: Forge
    pr_number: int
    head_sha: str
    tree: Path
    db_path: Path


Review = Callable[[ReviewRequest], TeamOutcome]


class BenchOrchestrator(ReviewTeamOrchestrator):
    """The review team of a dry run that also never fires the escalation routine.

    ``review_pr`` fires the proposed-escalation dossier whenever the
    routine secrets are set, dry run or not; a benchmark reviewing
    planted defects must not page anyone.
    """

    def _fire_proposed_escalation_dossier(self, pr_number: int) -> None:
        """Do nothing: a benchmark never escalates."""


def pipeline_review(request: ReviewRequest) -> TeamOutcome:
    """Review ``request`` with the real four-reviewer pipeline, in dry run."""
    return BenchOrchestrator(
        forge=request.forge,
        db_path=request.db_path,
        post_to_github=False,
        repo_root=request.tree,
        head_sha=request.head_sha,
        config_root=request.tree,
    ).review_pr(request.pr_number)


def _git(root: Path, *args: str) -> str:
    result = spawn(["git", *_BENCH_IDENTITY, *args], cwd=root, timeout_s=_GIT_TIMEOUT_S)
    if not result.ok:
        raise ForgeError(f"git {' '.join(args)} failed in {root}: {result.stderr.strip()}")
    return result.stdout.strip()


def materialise_local(case: Case, workdir: Path) -> tuple[Path, str, str]:
    """Build the base and head commits of a local case under ``workdir``.

    Returns:
        ``(tree, base_sha, head_sha)``; the tree is checked out at head.

    Raises:
        ForgeError: The patch does not apply, or git failed.
    """
    fixture, patch = case.source.fixture, case.source.patch
    if fixture is None or patch is None:
        raise ForgeError(f"case {case.case}: not a local source")
    tree = workdir / "tree"
    shutil.copytree(case.resolve(fixture), tree)
    _git(tree, "init", "-q", "-b", "base")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "base")
    base = _git(tree, "rev-parse", "HEAD")
    _git(tree, "apply", "--index", str(case.resolve(patch)))
    _git(tree, "commit", "-q", "-m", f"bench: {case.case}")
    head = _git(tree, "rev-parse", "HEAD")
    return tree, base, head


def severity_index(team: TeamOutcome) -> dict[SeverityKey, str]:
    """Map every reviewer comment of ``team`` to its severity."""
    return {
        (outcome.role.value, comment.file, comment.line, comment.body[:500]): comment.severity
        for outcome in team.reviews
        for comment in outcome.comments
    }


def ledger_severity_index(db_path: Path, pr_number: int) -> dict[SeverityKey, str]:
    """Map the comments ``pr_reviews`` recorded for ``pr_number`` to their severity.

    Returns an empty map when the ledger has no ``pr_reviews`` table.
    """
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        if not inspect(engine).has_table("pr_reviews"):
            return {}
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT role, comments_json FROM pr_reviews WHERE pr_number = :n"),
                {"n": pr_number},
            ).all()
    finally:
        engine.dispose()
    index: dict[SeverityKey, str] = {}
    for role, comments_json in rows:
        for comment in json.loads(comments_json):
            key = (role, comment["file"], int(comment["line"]), comment["body"][:500])
            index[key] = comment["severity"]
    return index


def record(
    findings: list[Finding], severities: dict[SeverityKey, str], reviewers: list[str]
) -> list[RecordedFinding]:
    """Turn ledger rows into recorded findings, kept for ``reviewers`` only.

    A row's severity is its comment's (``blocker`` … ``nit``); a row no
    comment produced (an oversized file) keeps the ledger's.
    """
    kept: list[RecordedFinding] = []
    for row in findings:
        if row.finder not in reviewers:
            continue
        key = (row.finder, row.file, row.line_start, row.claim)
        kept.append(
            RecordedFinding(
                fingerprint=fingerprint(
                    reviewer=row.finder,
                    claim_type=row.claim_type.value,
                    file=row.file,
                    line_start=row.line_start,
                ),
                reviewer=row.finder,
                claim_type=row.claim_type.value,
                severity=severities.get(key, row.severity.value),
                file=row.file,
                line_start=row.line_start,
                line_end=row.line_end,
                claim=row.claim,
                status=row.status.value,
                reason=row.verification_result,
            )
        )
    return kept


def pr_url(repo: str, pr: int) -> str:
    """Return the web link of PR / MR ``pr`` of ``repo``, the forge inferred from its host."""
    return pr_link(web_url(repo), pr, infer_forge_from_url(repo) or "github")


class _CaseSkippedError(Exception):
    """The case cannot be reviewed; the message is the reason recorded."""


class BenchRunner:
    """Run a corpus case by case.

    Args:
        corpus: The corpus.
        provider: The provider label recorded; the caller selects it (``--provider``).
        reviewers: The reviewers whose findings are kept.
        review: The review call (the real pipeline by default).
        preflight: Raises when the provider cannot be used; runs once, before
            any case, unless a dry run or a corpus of ledger cases only.
        ledger_db: The ledger ``ledger`` cases are read from.
        checkout: The repository ``forge`` cases are PRs of.
        dry_run: Materialise every case, call no model, record no finding.
        announce: Receives one progress line per case.
    """

    def __init__(
        self,
        corpus: Corpus,
        *,
        provider: str,
        reviewers: list[str] | None = None,
        review: Review = pipeline_review,
        preflight: Callable[[], object] = require_provider_ready,
        ledger_db: Path | None = None,
        checkout: Path | None = None,
        dry_run: bool = False,
        announce: Callable[[str], None] | None = None,
    ) -> None:
        self._corpus = corpus
        self._provider = provider
        self._reviewers = list(reviewers or REVIEWERS)
        self._review = review
        self._preflight = preflight
        self._ledger_db = ledger_db
        self._checkout = checkout or Path.cwd()
        self._dry_run = dry_run
        self._announce = announce or (lambda _line: None)
        self._forge: Forge | None = None

    def run(self) -> BenchResult:
        """Run every case and return the result (``partial`` when a case failed).

        Raises:
            BenchUnavailableError: The provider cannot be used (before any case).
        """
        started = datetime.now(UTC)
        result = BenchResult(
            corpus=self._corpus.name,
            corpus_root=str(self._corpus.root),
            corpus_sha=self._corpus.sha,
            provider=self._provider,
            reviewers=self._reviewers,
            measured_on=started.date(),
            started_at=started,
            dry_run=self._dry_run,
        )
        needs_model = any(case.source.kind != "ledger" for case in self._corpus.cases)
        if needs_model and not self._dry_run:
            try:
                self._preflight()
            except (GatewayConfigError, ProviderConfigError) as exc:
                raise BenchUnavailableError(f"provider {self._provider}: {exc}") from exc
        for number, case in enumerate(self._corpus.cases, start=1):
            self._announce(f"bench: case {number}/{len(self._corpus.cases)} {case.case}")
            started_case = time.monotonic()
            try:
                run = self._run_case(case, number)
            except _CaseSkippedError as exc:
                run = CaseRun(case=case.case, kind=case.source.kind, skipped=str(exc))
            except Exception as exc:
                result.partial = True
                _log.warning(
                    "bench.case_failed", case=case.case, exc=type(exc).__name__, message=str(exc)
                )
                self._announce(
                    f"bench: case {case.case} failed ({type(exc).__name__}: {exc}); stopping"
                )
                break
            if not run.wall_s:
                run.wall_s = round(time.monotonic() - started_case, 1)
            if run.verdict == ReviewVerdict.ERROR.value:
                result.partial = True
            result.cases.append(run)
        return result

    def _run_case(self, case: Case, number: int) -> CaseRun:
        source = case.source
        if source.kind == "ledger":
            return self._ledger_case(case)
        with tempfile.TemporaryDirectory(prefix=f"repoach-bench-{case.case}-") as tmp:
            workdir = Path(tmp)
            if source.kind == "local":
                tree, base, head = materialise_local(case, workdir)
                forge: Forge = LocalDiffForge(
                    tree, number=number, base_sha=base, head_sha=head, title=case.description
                )
                return self._reviewed(case, forge, number, head, tree, workdir, url=None)
            return self._forge_case(case, workdir)

    def _forge_case(self, case: Case, workdir: Path) -> CaseRun:
        source = case.source
        pr = source.pr
        repo = source.repo
        if pr is None or repo is None:
            raise _CaseSkippedError("forge source without pr / repo")
        forge = self._forge or select_forge(cwd=self._checkout)
        self._forge = forge
        try:
            sha = require_head_sha(resolve_fresh_head(forge, pr, repo_root=self._checkout), pr)
        except Exception as exc:
            raise _CaseSkippedError(f"head unavailable: {exc}") from exc
        if source.head_sha and source.head_sha != sha:
            raise _CaseSkippedError(
                f"head moved: case pins {source.head_sha[:7]}, forge serves {sha[:7]}"
            )
        with materialise_head(
            forge, pr, sha, repo_root=self._checkout, state_dir=workdir, keep=False
        ) as tree:
            return self._reviewed(case, forge, pr, sha, tree.root, workdir, url=pr_url(repo, pr))

    def _reviewed(
        self,
        case: Case,
        forge: Forge,
        number: int,
        head: str,
        tree: Path,
        workdir: Path,
        *,
        url: str | None,
    ) -> CaseRun:
        run = CaseRun(case=case.case, kind=case.source.kind, head_sha=head, pr_url=url)
        if self._dry_run:
            return run
        db_path = workdir / "bench.db"
        started = time.monotonic()
        with usage_run("bench run", number) as usage:
            team = self._review(
                ReviewRequest(
                    forge=forge, pr_number=number, head_sha=head, tree=tree, db_path=db_path
                )
            )
        run.wall_s = round(time.monotonic() - started, 1)
        total = totals(usage.rows)
        run.tokens = total.total_tokens
        run.calls = total.calls
        run.errors = total.errors
        run.verdict = team.final_verdict.value
        rows = [row for row in fetch_findings(db_path, number) if row.head_sha == head]
        run.findings = record(rows, severity_index(team), self._reviewers)
        return run

    def _ledger_case(self, case: Case) -> CaseRun:
        source = case.source
        pr, repo, head = source.pr, source.repo, source.head_sha
        if pr is None or repo is None or head is None:
            raise _CaseSkippedError("ledger source without pr / repo / head_sha")
        db_path = self._ledger_db or Path(get_settings().db_path)
        if not db_path.exists():
            raise _CaseSkippedError(f"ledger {db_path} does not exist")
        recorded_head = "" if head == UNRECORDED_HEAD else head
        rows = [row for row in fetch_findings(db_path, pr) if row.head_sha == recorded_head]
        if not rows:
            raise _CaseSkippedError(f"no finding recorded for {pr} at {head[:10]} in {db_path}")
        return CaseRun(
            case=case.case,
            kind="ledger",
            head_sha=head,
            pr_url=pr_url(repo, pr),
            findings=record(rows, ledger_severity_index(db_path, pr), self._reviewers),
        )


def run_bench(
    corpus: Corpus,
    provider: str,
    reviewers: list[str] | None = None,
    *,
    dry_run: bool = False,
    ledger_db: Path | None = None,
    announce: Callable[[str], None] | None = None,
) -> BenchResult:
    """Run ``corpus`` on ``provider`` with the real pipeline (see :class:`BenchRunner`)."""
    return BenchRunner(
        corpus,
        provider=provider,
        reviewers=reviewers,
        dry_run=dry_run,
        ledger_db=ledger_db,
        announce=announce,
    ).run()


__all__ = [
    "BenchOrchestrator",
    "BenchRunner",
    "BenchUnavailableError",
    "ReviewRequest",
    "materialise_local",
    "pipeline_review",
    "pr_url",
    "record",
    "run_bench",
    "severity_index",
]
