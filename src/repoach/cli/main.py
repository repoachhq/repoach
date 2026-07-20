"""Top-level Typer CLI — the PR review factory.

One operational capability: ``repoach review pr/report/fix/merge``
plus the ``repoach develop`` alias used to run an autonomous
Developer session from a spec. The rest of the system is rebuilt on
top of this factory.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import typer

from ..arch.cli import arch_app
from ..core.logging import configure_logging
from ..lint import edge_honesty
from .chain_status import chain_status
from .release_cmds import release_app
from .review_cmds import review_app, review_develop, review_plan

app = typer.Typer(
    help="Repoach CLI (genesis reset — review factory only)",
    no_args_is_help=True,
)
app.add_typer(review_app, name="review")
app.add_typer(release_app, name="release")


@arch_app.command("check")
def arch_check(
    base: str = typer.Option("develop", "--base", help="Diff against this ref (three-dot)."),
    staged: bool = typer.Option(False, "--staged", help="Check the staged index (pre-commit)."),
    specs_dir: Path = typer.Option(
        Path("docs/specs"), "--specs-dir", help="Directory of spec markdown files."
    ),
) -> None:
    """Fail when a changed file's couplings are missing from its depends_on."""
    repo_root = Path(__file__).resolve().parents[3]
    report = edge_honesty.run(base=base, staged=staged, specs_dir=specs_dir, repo_root=repo_root)
    for line in edge_honesty.report_lines(report):
        typer.echo(line, err=True)
    if not report.ok:
        raise typer.Exit(code=1)
    typer.echo("edge-honesty: ok")


app.add_typer(arch_app, name="arch")

app.command(name="develop")(review_develop)
app.command(name="plan")(review_plan)
app.command(name="chain-status")(chain_status)


@app.callback()
def _bootstrap() -> None:
    """Configure logging once for every CLI invocation."""
    configure_logging()


@app.command("version")
def show_version() -> None:
    """Print the installed Repoach version."""
    from .. import __version__

    typer.echo(__version__)


def _probe_client() -> httpx.AsyncClient:
    """Return an httpx.AsyncClient for NIM probes and credits checks.

    Extracted as a module-level factory so tests can override it with
    a MockTransport-backed client.
    """
    return httpx.AsyncClient()


@app.command("monitor-chains")
def monitor_chains(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
    slow_threshold: float = typer.Option(
        8.0, "--slow-threshold", help="Latency (s) above which a head is reported 'slow'."
    ),
    max_tokens: int = typer.Option(16, "--max-tokens", help="Probe completion budget."),
    no_persist: bool = typer.Option(
        False, "--no-persist", help="Skip writing the sweep to the nim_health_probe table."
    ),
    db_path: str | None = typer.Option(
        None, "--db-path", help="Override the SQLite path (default: the configured review DB)."
    ),
) -> None:
    """Probe each capability tier's NIM head model and report its health.

    Surfaces a drifting head — the HTTP-200-but-empty thinking-leak or a
    latency spike — that the proxy's budget-retry + failover would
    otherwise hide. Exits ``0`` when every NIM head is ok/slow/skipped,
    ``1`` when any is empty or error, so a cron/monitor can gate on it.
    """
    import asyncio
    import dataclasses
    import json as json_module
    from datetime import UTC, datetime
    from pathlib import Path

    from ..core.config import get_settings
    from ..core.logging import get_logger
    from ..health.credits import CreditsSnapshot, fetch_openrouter_credits
    from ..llm_proxy.config.settings import Settings
    from ..review.chain_health import check_tier_heads, is_degraded
    from ..review.chain_health_store import record_probes

    settings = Settings()

    async def _run() -> tuple[list, CreditsSnapshot | None]:
        async with _probe_client() as client:
            results = await check_tier_heads(
                settings,
                client=client,
                slow_threshold_s=slow_threshold,
                max_tokens=max_tokens,
            )
            if settings.open_router_api_key:
                credits = await fetch_openrouter_credits(
                    settings.open_router_api_key, client=client
                )
            else:
                credits = None
            return results, credits

    results, credits = asyncio.run(_run())

    if not no_persist:
        target = Path(db_path) if db_path else Path(get_settings().db_path)
        written = record_probes(target, results, recorded_at=datetime.now(UTC))
        get_logger(__name__).info("nim_health_persisted", rows=written, db_path=str(target))

    floor = settings.credits_floor_usd
    if not settings.open_router_api_key:
        credits_status = "skipped"
    elif credits is None:
        credits_status = "unavailable"
    elif credits.remaining < floor:
        credits_status = "LOW"
    else:
        credits_status = "ok"

    log = get_logger(__name__)
    credits_log: dict[str, object] = {"floor": floor, "status": credits_status}
    if credits is not None:
        credits_log["remaining"] = credits.remaining
    if credits_status == "LOW":
        log.warning("openrouter_credits", **credits_log)
    else:
        log.info("openrouter_credits", **credits_log)

    if json_output:
        results_data: list[object] = [dataclasses.asdict(r) for r in results]
        credits_elem: dict[str, object] = {
            "kind": "credits",
            "status": credits_status,
            "total_credits": credits.total_credits if credits else None,
            "total_usage": credits.total_usage if credits else None,
            "remaining": credits.remaining if credits else None,
            "floor": floor,
        }
        results_data.append(credits_elem)
        typer.echo(json_module.dumps(results_data, indent=2))
    else:
        for r in results:
            latency = f"{r.latency_s:.2f}s" if r.latency_s is not None else "—"
            typer.echo(f"{r.tier:<7} {r.status:<8} {latency:>8}  {r.model}  {r.detail}")
        if credits is not None:
            typer.echo(
                f"credits open_router [remaining={credits.remaining} floor={floor}] {credits_status}"
            )
        else:
            typer.echo(f"credits open_router {credits_status}")

    if any(is_degraded(r.status) for r in results) or credits_status == "LOW":
        raise typer.Exit(code=1)


@app.command("autopilot")
def autopilot(
    apply: bool = typer.Option(
        False, "--apply", help="Write chains.env (default: shadow journal only)."
    ),
    chains_path: str = typer.Option(
        "chains.env", "--chains-path", help="Path to the chains.env to rewrite."
    ),
    prior_metric: str | None = typer.Option(
        None, "--prior-metric", help="Benchmark metric for the cold-start prior."
    ),
    db_path: str | None = typer.Option(
        None, "--db-path", help="Override the SQLite path (default: the configured review DB)."
    ),
) -> None:
    """Run one Chain Autopilot cycle: sweep, attribute, decide, plan, apply.

    Shadow by default — the planned mutations + cold-starts are journalled to the
    audit log with ``applied=False`` and ``chains.env`` is left untouched. Pass
    ``--apply`` (or set ``FEROVA_CHAINPILOT_APPLY_ENABLED=true``) to actually write,
    atomically and with a ``.bak`` backup.
    """
    import asyncio
    from datetime import UTC, datetime

    import httpx

    from ..core.config import get_settings
    from ..core.logging import get_logger
    from ..llm_proxy.config.settings import Settings
    from ..llm_proxy.providers.benchmark_equivalences import load_equivalence_table
    from ..llm_proxy.providers.benchmark_prior import load_benchmark_ranking
    from ..review.chain_loop import CycleReport, run_autopilot_cycle

    settings = Settings()
    enabled = apply or settings.chainpilot_apply_enabled
    target_db = Path(db_path) if db_path else Path(get_settings().db_path)
    ranking = load_benchmark_ranking()
    equivalences = load_equivalence_table()

    async def _run() -> CycleReport:
        async with httpx.AsyncClient() as client:
            return await run_autopilot_cycle(
                settings=settings,
                client=client,
                db_path=target_db,
                chains_path=Path(chains_path),
                ranking=ranking,
                equivalences=equivalences,
                recorded_at=datetime.now(UTC),
                prior_metric=prior_metric,
                max_mutations=settings.chainpilot_max_mutations,
                enabled=enabled,
            )

    report = asyncio.run(_run())
    get_logger(__name__).info(
        "chainpilot_cycle",
        matrix_cells=report.matrix_cells,
        faults=report.faults,
        mutations=report.mutations,
        cold_starts=report.cold_starts,
        written=report.written,
        journaled=report.journaled,
    )
    typer.echo(
        f"autopilot: cells={report.matrix_cells} faults={report.faults} "
        f"mutations={report.mutations} cold_starts={report.cold_starts} "
        f"written={report.written} journaled={report.journaled}"
    )


@app.command("chains-audit")
def chains_audit() -> None:
    """Print chain-head thinking-class findings and always exit 0.

    Reads the configured ``MODEL_OPUS`` / ``MODEL_SONNET`` / ``MODEL_HAIKU``
    slots, parses each into a chain of model ids, and reports every chain
    whose head model is classified ``reasoner`` or ``unknown`` (rule #1 of
    ``chains.env``). Report-only slice — enforcement is a later decision.
    """
    from ..llm_proxy.config.settings import Settings
    from ..llm_proxy.providers.thinking_audit import audit_chain_thinking
    from ..llm_proxy.routing.chain import Chain

    settings = Settings()

    chains: dict[str, list[str]] = {}
    for chain_name, spec in (
        ("opus", settings.model_opus),
        ("sonnet", settings.model_sonnet),
        ("haiku", settings.model_haiku),
    ):
        if not spec or not spec.strip():
            continue
        chains[chain_name] = [str(ref) for ref in Chain.parse(spec).refs]

    for finding in audit_chain_thinking(chains):
        typer.echo(finding)


@app.command("regenerate-chains")
def regenerate_chains(
    apply: bool = typer.Option(
        False, "--apply", help="Write chains.env (default: shadow — compute + log only)."
    ),
    chains_path: str = typer.Option(
        "chains.env", "--chains-path", help="Path to the chains.env to regenerate."
    ),
    db_path: str | None = typer.Option(
        None, "--db-path", help="Override the SQLite path (default: the configured review DB)."
    ),
) -> None:
    """Regenerate chains.env model-first: choose models by capability, expand to providers.

    Gathers the live Artificial Analysis ranking, the provider matrix, the
    equivalence table and the per-cell probe latency, then rebuilds each tier
    chain (Claude-anchored selection → NIM-first provider expansion → claude_code
    tail). Shadow by default — pass ``--apply`` (or set
    ``FEROVA_CHAINPILOT_APPLY_ENABLED=true``) to write atomically with a ``.bak``.

    Runs alongside the Chain Autopilot; it does not modify the autopilot loop.
    """
    import asyncio

    import httpx

    from ..core.config import get_settings
    from ..core.logging import get_logger
    from ..llm_proxy.config.settings import Settings
    from ..llm_proxy.routing.chain_regen import gather_and_regenerate

    settings = Settings()
    enabled = apply or settings.chainpilot_apply_enabled
    target_db = Path(db_path) if db_path else Path(get_settings().db_path)

    async def _run() -> object:
        async with httpx.AsyncClient() as client:
            return await gather_and_regenerate(
                settings,
                client=client,
                chains_path=Path(chains_path),
                db_path=target_db,
                enabled=enabled,
            )

    result = asyncio.run(_run())
    get_logger(__name__).info(
        "regenerate_chains",
        changed=result.changed,
        written=result.written,
    )
    typer.echo(
        f"regenerate-chains: changed={result.changed} written={result.written} "
        f"opus={len(result.chains.get('opus', ()))} "
        f"sonnet={len(result.chains.get('sonnet', ()))} "
        f"haiku={len(result.chains.get('haiku', ()))}"
    )


memory_app = typer.Typer(
    help="Persistent-memory (agentmemory) utilities — builder + review scopes.",
    no_args_is_help=True,
)
app.add_typer(memory_app, name="memory")


@memory_app.command("seed-builder")
def memory_seed_builder() -> None:
    """Load the curated builder lessons into the agentmemory builder scope."""
    from ..review.builder_memory import seed_builder_memory

    written = seed_builder_memory()
    typer.echo(f"seeded {written} builder lesson(s)")


@memory_app.command("recall-builder")
def memory_recall_builder(
    query: str = typer.Argument(..., help="Search query against the builder memory scope."),
) -> None:
    """Recall and print builder-scoped lessons for a query (inspection)."""
    from ..review.builder_memory import recall_builder_lessons

    lessons = recall_builder_lessons(query)
    if not lessons:
        typer.echo("(no lessons recalled)")
        return
    for lesson in lessons:
        typer.echo(f"- {lesson}")


@memory_app.command("seed-review")
def memory_seed_review() -> None:
    """Load the curated review trap lessons into the agentmemory review scope."""
    from ..review.review_memory import seed_review_memory

    written = seed_review_memory()
    typer.echo(f"seeded {written} review lesson(s)")


@memory_app.command("recall-review")
def memory_recall_review(
    query: str = typer.Argument(..., help="Search query against the review memory scope."),
) -> None:
    """Recall and print review-scoped lessons for a query (inspection)."""
    from ..review.review_memory import recall_review_lessons

    lessons = recall_review_lessons(query)
    if not lessons:
        typer.echo("(no lessons recalled)")
        return
    for lesson in lessons:
        typer.echo(f"- {lesson}")


def main() -> None:
    """Entry point used by the ``repoach`` console script."""
    app()


if __name__ == "__main__":
    main()
