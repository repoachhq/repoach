"""``repoach bench run|report|label`` and ``repoach digest`` (SP-PRECISION-MEASUREMENT).

``bench run`` reviews every case of a corpus and writes the result file;
``bench report`` scores a result against the corpus labels read now
(``bench score`` is the same command); ``bench label`` writes an
unlabelled stub per new finding into the case files, or asks for each
label in turn with ``--interactive``. ``digest`` prints what the gate
refused over a period.

Exit codes: 0 done; 2 corpus, result, provider or ledger unavailable,
or a refused ``--readme-line``; 4 ``labels stale`` (fewer than 70 % of
the labelled fingerprints found again, A1).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import typer

from ..bench.corpus import (
    Corpus,
    CorpusError,
    Label,
    add_stubs,
    load_corpus,
    write_case,
)
from ..bench.digest import digest as read_digest
from ..bench.digest import render_json, render_markdown, web_url
from ..bench.result import BenchResult, ResultError, load_result, save_result
from ..bench.runner import BenchUnavailableError, run_bench
from ..bench.score import (
    REVIEWERS,
    ReadmeLineRefusedError,
    Scorecard,
    readme_line,
    render_report,
    score,
)
from ..core.config import get_settings
from ..core.paths import project_root
from ..forge.runner import spawn
from ..project.resolve import infer_forge_from_url, resolve_project_config
from ..providers.errors import ProviderConfigError
from ..providers.select import resolve_route
from ..providers.usage import parse_since
from .review_cmds import apply_provider_flag

README_START = "<!-- bench:precision:start -->"
README_END = "<!-- bench:precision:end -->"

bench_app = typer.Typer(
    help="Precision and recall of findings on a labelled corpus.",
    no_args_is_help=True,
)


def _fail(message: str, code: int = 2) -> typer.Exit:
    typer.echo(f"repoach: {message}", err=True)
    return typer.Exit(code=code)


def _corpus(path: Path) -> Corpus:
    try:
        return load_corpus(path)
    except CorpusError as exc:
        raise _fail(str(exc)) from exc


def _result(path: Path) -> BenchResult:
    try:
        return load_result(path)
    except ResultError as exc:
        raise _fail(str(exc)) from exc


def _reviewers(value: str | None) -> list[str]:
    if not value:
        return list(REVIEWERS)
    names = [name.strip().lower() for name in value.split(",") if name.strip()]
    unknown = sorted(set(names) - set(REVIEWERS))
    if unknown:
        raise _fail(f"--reviewers: unknown {', '.join(unknown)}; use {', '.join(REVIEWERS)}")
    return names


@bench_app.command("run")
def bench_run(
    corpus_arg: Path | None = typer.Argument(
        None, metavar="[CORPUS]", help="Corpus directory or case file."
    ),
    corpus_opt: Path | None = typer.Option(None, "--corpus", help="Same as the argument."),
    provider: str | None = typer.Option(
        None, "--provider", help="Provider of every review (outranks REPOACH_PROVIDER)."
    ),
    reviewers: str | None = typer.Option(
        None, "--reviewers", help="Comma-separated reviewers whose findings are kept."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate and materialise every case; call no model."
    ),
    results_dir: Path = typer.Option(
        Path("bench/results"), "--results-dir", help="Where the result file is written."
    ),
    ledger_db: Path | None = typer.Option(
        None, "--ledger-db", help="Ledger of the ledger cases (default: the review database)."
    ),
) -> None:
    """Review every case of a corpus and write ``<date>_<corpus>_<provider>.json``.

    Every review is a dry run on a throw-away ledger. Prints the result
    path on stdout and the unlabelled count on stderr.
    """
    path = corpus_opt or corpus_arg
    if path is None:
        raise _fail("bench run: name a corpus (argument or --corpus)")
    corpus = _corpus(path)
    kept = _reviewers(reviewers)
    apply_provider_flag(provider)
    try:
        label = resolve_route(Path.cwd()).label
    except ProviderConfigError as exc:
        raise _fail(str(exc)) from exc
    try:
        result = run_bench(
            corpus,
            label,
            kept,
            dry_run=dry_run,
            ledger_db=ledger_db,
            announce=lambda line: typer.echo(line, err=True),
        )
    except BenchUnavailableError as exc:
        raise _fail(str(exc)) from exc
    written = save_result(result, results_dir)
    card = score(result, corpus)
    typer.echo(
        f"bench: {len(result.cases)} cases, {sum(len(c.findings) for c in result.cases)} "
        f"findings, {len(card.unlabelled)} unlabelled"
        f"{', partial' if result.partial else ''}",
        err=True,
    )
    typer.echo(str(written))
    if card.labels_stale:
        raise _fail(f"labels stale: match rate {card.match_rate.text()} < 70 %", code=4)


def _scorecard(result_path: Path, corpus_path: Path | None) -> Scorecard:
    result = _result(result_path)
    return score(result, _corpus(corpus_path or Path(result.corpus_root)))


def _replace_block(readme: Path, block: str) -> None:
    text = readme.read_text(encoding="utf-8")
    start, end = text.find(README_START), text.find(README_END)
    if start < 0 or end < start:
        raise _fail(f"{readme}: no {README_START} … {README_END} block to replace")
    updated = text[: start + len(README_START)] + "\n" + block + text[end:]
    readme.write_text(updated, encoding="utf-8")


def bench_report(
    result_path: Path = typer.Argument(..., metavar="RESULT", help="A bench result file."),
    corpus_path: Path | None = typer.Option(
        None, "--corpus", help="Read the labels from this corpus (default: the result's)."
    ),
    markdown: bool = typer.Option(False, "--markdown", help="Print Markdown tables."),
    readme: bool = typer.Option(
        False, "--readme-line", help="Print only the published line (refused below 100 labels)."
    ),
    update_readme: Path | None = typer.Option(
        None,
        "--update-readme",
        help="Replace the bench:precision block of this file with the Markdown report.",
    ),
) -> None:
    """Print the per-reviewer and per-severity tables of a result.

    Header columns: ``reviewer | TP | FP | unsure | precision | refuter
    precision | recall``. ``--readme-line`` exits 2 on a partial run, a
    dry run, or fewer than 100 labelled findings (``corpus too small``).
    """
    card = _scorecard(result_path, corpus_path)
    if readme:
        try:
            typer.echo(readme_line(card))
        except ReadmeLineRefusedError as exc:
            raise _fail(str(exc)) from exc
        return
    lines = render_report(card, markdown=markdown or update_readme is not None)
    if update_readme is not None:
        _replace_block(update_readme, "\n".join(lines) + "\n")
        typer.echo(f"bench: {update_readme} updated", err=True)
        return
    for line in lines:
        typer.echo(line)
    if card.unlabelled:
        typer.echo(
            f"bench: {len(card.unlabelled)} findings unlabelled; `repoach bench label "
            f"{result_path}` lists them",
            err=True,
        )


bench_app.command("report")(bench_report)
bench_app.command("score", hidden=True)(bench_report)


def _git_user() -> str:
    result = spawn(["git", "config", "user.name"], cwd=Path.cwd(), timeout_s=10)
    return result.stdout.strip() if result.ok else ""


@bench_app.command("label")
def bench_label(
    result_path: Path = typer.Argument(..., metavar="RESULT", help="A bench result file."),
    corpus_path: Path | None = typer.Option(
        None, "--corpus", help="Write to this corpus (default: the result's)."
    ),
    interactive: bool = typer.Option(
        False, "--interactive", help="Ask for each label in turn (t / f / u, s skips, q stops)."
    ),
    labeller: str | None = typer.Option(
        None, "--labeller", help="Who labels (default: git config user.name)."
    ),
) -> None:
    """Add an unlabelled stub per new finding to its case file, or label them one by one.

    A stub is ``{fingerprint, label: null, hint}``; set ``label`` to
    ``true``, ``false`` or ``unsure`` and fill ``labeller`` and
    ``labelled_on``. A private case's stubs carry no hint: read the
    claim from the result file.
    """
    result = _result(result_path)
    corpus = _corpus(corpus_path or Path(result.corpus_root))
    card = score(result, corpus)
    by_case: dict[str, list[tuple[str, str]]] = {}
    for item in card.unlabelled:
        f = item.finding
        hint = (
            f"{f.reviewer} {f.severity} {f.file}:{f.line_start}: {' '.join(f.claim.split())[:160]}"
        )
        by_case.setdefault(item.case, []).append((f.fingerprint, hint))
    stubs = 0
    for case_id, entries in by_case.items():
        try:
            case = corpus.case(case_id)
            added = add_stubs(case, entries)
            if added:
                write_case(case)
        except CorpusError as exc:
            raise _fail(str(exc)) from exc
        stubs += added
    typer.echo(
        f"bench: {stubs} stubs written, {len(card.unlabelled)} findings unlabelled", err=True
    )
    if not interactive:
        return
    who = labeller or _git_user()
    if not who:
        raise _fail("bench label --interactive: name the labeller (--labeller)")
    answers: dict[str, Label] = {"t": "true", "f": "false", "u": "unsure"}
    for item in card.unlabelled:
        f = item.finding
        typer.echo(
            f"\n[{item.case}] {f.reviewer} {f.severity} {f.claim_type} {f.file}:{f.line_start}"
            f" ({f.status})\n{f.claim}"
        )
        answer = typer.prompt("label t/f/u (s skip, q stop)", default="s").strip().lower()
        if answer == "q":
            break
        if answer not in answers:
            continue
        case = corpus.case(item.case)
        for entry in case.findings:
            if entry.fingerprint == f.fingerprint:
                entry.label = answers[answer]
                entry.labeller = who
                entry.labelled_on = date.today()
        write_case(case)


def _origin_url() -> str:
    result = spawn(["git", "remote", "get-url", "origin"], cwd=Path.cwd(), timeout_s=10)
    return web_url(result.stdout) if result.ok else ""


def digest(
    since: str = typer.Option(
        "7d", "--since", help="Start of the period: a duration (7d, 2w) or a date (2026-10-13)."
    ),
    output: str = typer.Option("md", "--format", help="md or json."),
    db: Path | None = typer.Option(None, "--db", help="Ledger (default: the review database)."),
    repo_url: str | None = typer.Option(
        None, "--repo-url", help="Repository URL for the links (default: the origin remote)."
    ),
) -> None:
    """Print what the gate refused since a date: refuted findings, refused LGTMs, later merges."""
    if output not in ("md", "json"):
        raise _fail(f"--format {output!r}: use md or json")
    try:
        start = parse_since(since, now=datetime.now(UTC))
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    url = web_url(repo_url) if repo_url else _origin_url()
    if not url:
        raise _fail("digest: no origin remote to link PRs to; pass --repo-url")
    forge = infer_forge_from_url(url) or resolve_project_config(project_root(Path.cwd())).forge
    report = read_digest(db or Path(get_settings().db_path), start, repo_url=url, forge=forge)
    typer.echo(render_json(report) if output == "json" else render_markdown(report), nl=False)
    if output == "json":
        typer.echo("")


__all__ = ["README_END", "README_START", "bench_app", "digest"]
