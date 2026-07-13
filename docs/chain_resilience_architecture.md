# Chain resilience — architecture

> Status: **design v1, adversarial-panel reviewed** (2026-07-11).
> Umbrella for the resilience spec family (wave 1 specs to be written
> after operator review of this document). Diagram:
> `chain_resilience_architecture.svg` (+ `.png`). Companion designs:
> `model_first_chains_architecture.md` (chains.env as generated
> artifact), `chain_autopilot_architecture.md`, `tech_debt.md` items
> 1 and 7.

## Objective

The proxy's failover chains must **detect**, **contain**, and
eventually **repair** upstream degradation without a human noticing
first — and everything the system knows must reach a surface the
operator provably sees.

## Incident context (2026-07-10, PR #76)

The SONNET head `nvidia_nim/minimaxai/minimax-m3` flapped for a week
(~19% error / ~12% slow at 12–15 s / ~6% empty per day in
`nim_health_probe`) then went hard-400 (`DEGRADED function cannot be
invoked`). OpenRouter credits were exhausted (402 on every ref), so
each sonnet request walked three dead hops (~9 s) before being served
by chain entry #4. The chainpilot shadow cycle kept concluding
`changed=False` on cells last swept 2026-06-30. Repair was manual
(evidence probes → PR #76 → factory auto-merge → service restart).

The post-mortem's sharpest fact: **this was a surfacing failure, not
a detection failure.** Incident day alone produced 130
`proxy_chain_failover_fired` warnings and daily degraded probe rows
all week — in logs and tables no surface exposed. Any design whose
alerts terminate in another log repeats the failure.

## Design principles

1. **Surface first.** Wave 1's notification terminus is a surface the
   operator already reads daily (the session-start hook), not a new
   channel. The custom push-notification system is a wave-2 design in
   its own right (operator decision: no Claude Code routines — the
   15/day quota does not fit a 96-probe/day monitor).
2. **Slowness is a live-dispatch fault.** An HTTP-200 completion in
   12–15 s never triggers failover and even *resets* the breaker
   (`recover()`), so a degraded head keeps re-promoting itself. Live
   dispatch protects callers; this deliberately diverges from the
   offline-probe doctrine ("slowness is not a fault",
   `providers/attribution.py`), which assesses capability.
3. **The failure domain is the (provider, model) cell**, not the
   provider. NIM served glm-5.2 and deepseek-v4-pro perfectly while
   minimax-m3's function was DEGRADED. Provider-wide semantics apply
   only to account-class faults (401/402/403) — and are **parked**
   until the transport plumbing can even express them (see parking
   lot).
4. **Repair ships as reviewed PRs.** The chainpilot never writes
   `chains.env` in place; it proposes evidence-first PRs the factory
   reviews (aligned with the model-first target state where
   `chains.env` is a generated artifact). LLM bots never edit
   `chains.env` text (wave-1 guard).
5. **Detection must survive repair.** Today only NIM heads are
   probed (`STATUS_SKIPPED` otherwise): a repair that promotes a
   non-NIM head silently amputates monitoring for that tier. The
   blind spot must at minimum be loud (wave 1) and eventually closed
   by provider-agnostic head probes (wave 2).

## Schema

```
                     OPERATOR SURFACES (wave-1 terminus)
      session-start digest        GET /health            GitHub PRs
        `ferova chain-status`      (+ breaker,             (factory
             ▲    ▲                 + credits)              review)
             │    │                     ▲                      ▲
 ┌───────────┼────┼─────────────────────┼──────────────────────┼───────┐
 │ DETECTION │    │                     │                      │       │
 │           │    │                     │                      │       │
 │  monitor-chains (15-min timer) ──► nim_health_probe ────┐   │       │
 │    └─ credits GET, every cycle (W1.4) ──► /health       │   │       │
 │                                                         │   │       │
 │  [W2] rate-based windowed detector (x% of last M probes │   │       │
 │       not-ok, slow counted, hysteresis) ──► health_events   │       │
 │       ──► Notifier port ──► custom notification system  │   │       │
 └─────────────────────────────────────────────────────────┼───┼───────┘
 ┌─────────────────────────────────────────────────────────▼───┼───────┐
 │ CONTAINMENT (llm_proxy live dispatch)                        │       │
 │                                                              │       │
 │  client ──► ModelRouter.resolve_chain ◄── filter ◄── breaker │       │
 │               │                            (per-ModelRef,    │       │
 │               │                             probe-seeded     │       │
 │               │                             at startup)      │       │
 │               ├─ fault ──► classify ──► trip                 │       │
 │               │            (transient 120s / quarantine 6h / │       │
 │               │             terminal 7d, 3-strike escalation)│       │
 │               └─ success ──► slow? ──► slow-strike (W1.2:    │       │
 │                              separate k-of-n counter, never  │       │
 │                              escalates to hard quarantine)   │       │
 │                                                              │       │
 │  [parked] provider-scope trips for 402-class faults — dead   │       │
 │           code until upstream 4xx status is plumbed through  │       │
 │           the SSE-error path (PeekResult.upstream_error)     │       │
 └──────────────────────────────────────────────────────────────┼──────┘
 ┌──────────────────────────────────────────────────────────────┼──────┐
 │ REPAIR (chainpilot, 6-h timer, shadow today)                 │      │
 │                                                              │      │
 │  regenerate-chains:                                          │      │
 │    bounded in-cycle sweep (W1.3) ──► cell_health_probe       │      │
 │    ──► freshness guard (refuse on stale-newest, loud)        │      │
 │    ──► regen ──► [W3] propose PR + evidence dossier ─────────┘      │
 │                    (fingerprint embargo, invariants validator,      │
 │                     onset AND recovery hysteresis)                  │
 │                  [W3] merged ──► reload endpoint                    │
 │                        + breaker reconciliation                     │
 │                                                                     │
 │  chains.env = single source of truth                                │
 │    (W1.5: Coder path-blacklisted; human or regenerator only)        │
 └─────────────────────────────────────────────────────────────────────┘
```

## Wave 1 — beats the 2026-07-10 incident (5 small specs)

Every item is a single spec well inside autonomous-Developer capacity.

### W1.1 `ferova chain-status` digest → session-start hook

One command that reads what already exists and prints a per-tier
digest: last-24h probe rates from `nim_health_probe` **counting slow
in the bad column** ("sonnet: 19% error / 12% slow / 69% ok, avg slow
14 s"), current breaker snapshot (`GET /health`), `cell_health_probe`
freshness age, OpenRouter credits (live GET), and a loud
`tier unmonitored` warning when a tier's head is non-NIM (today's
probe skips it). Wired into the SessionStart hook (precedent:
`dream_check.py`); optionally a `safe_merge.sh` preflight line. This
is the wave-1 alert terminus: pull-based, stateless, no dedup logic.

### W1.2 Slow-strike breaker policy (shadow-first)

At the dispatch success hook (`services.py:291`, where full-completion
`attempt_latency_s` is already computed): a completion with
`latency_s > slow_latency_gate_s` **and** `output_tokens / latency_s <
slow_tps_floor` counts as a **slow strike** instead of `recover()`.
Constraints from panel review:

- **Separate counter** from hard failures — a slow strike never
  escalates to the 6-h hard quarantine (dedicated short TTL); two
  timeouts + one slow-but-served completion must NOT yield 6 h down.
- **k-of-n gate** (e.g. 3 of last 5 completions slow), not
  single-strike — heavy agentic requests legitimately run long.
- `PeekResult` gains `final_output_tokens: int | None` (computed
  today as a local in `_failover.py` but not carried); `None` →
  latency gate only or skip, decided in the spec.
- The **budget-retry success path** (`services.py:309-320`) bypasses
  the success hook today; the spec must give it the same
  recover-or-strike treatment explicitly.
- **Shadow-first**: a log-only mode flag for the first deployment
  window; the shadow log must be watched for mass slow-trips that
  would collapse chains onto the `claude_code` tail (Max quota).

### W1.3 Fresh cells: bounded in-cycle sweep + freshness guard

Root cause fix for `changed=False` on 10-day-old cells: the deployed
6-h timer runs `regenerate-chains`, a pure *reader* of
`cell_health_probe`; the only writer lives in the unscheduled
`ferova autopilot` path (`chain_loop.py:212-214`). Fix inline, not
with a new CLI/unit: `gather_and_regenerate` already holds the
matrix, the client and `db_path` — sweep before reading. Panel
constraints:

- **Bound the sweep.** A full-matrix sweep is ~490 cells (~340
  OpenRouter) of real completions per cycle ≈ 1,360 paid requests/day
  — from the account whose credit exhaustion is this design's
  incident context. Sweep only chain-relevant cells (current
  `chains.env` refs + the ranking candidate pool), with per-provider
  concurrency caps and pacing; state the expected per-sweep request
  count as an acceptance criterion.
- **429 is observer interference**, not cell death: exclude it from
  health classification (retry with backoff), so the sweep cannot
  poison the data that drives repair.
- **Skip paid-provider cells** when W1.4 reports credits below floor.
- Freshness guard at the read site (`chain_regen.py:94`): fetch with
  `since=lookback` and **refuse to conclude** (loud log, degraded
  exit) when the newest cell is older than `max_cell_age_h`.

### W1.4 Credits check, minimal

No new table, no new timer, no descriptor extension: one OpenRouter
`GET /api/v1/credits` inside the existing monitor-chains cycle (every
run — one GET per 15 min is negligible and keeps the CLI stateless),
two flat `FEROVA_*` threshold settings, a `credits` field on
`GET /health`, a digest line, degraded exit + loud log below floor.
History has no consumer today; current-value-vs-threshold is the whole
job.

### W1.5 `chains.env` Coder guard

Discovered hole: the Coder path whitelist blocks `.env`/`.env.*` by
basename and `FORBIDDEN_PATHS`/`PREFIXES` — `chains.env` passes. A
reviewer-driven LLM edit of the chain config, with zero probe
evidence, would sail through (CI never exercises chains.env against
live providers). Add it to the blacklist: chain changes come from
humans or from data-driven regeneration, never from LLM text edits.

## Wave 2 — custom notification system (design with operator)

The transport is the operator's own design (explicitly not Claude
Code routines). The detector and event store are shaped by the chosen
transport, with requirements already learned from panel review:

- **Rate-based windowed detection**, not N-consecutive: a subject is
  DOWN when > x% of its last M probes are not-ok (slow counted), UP
  under a lower hysteresis threshold. N-consecutive provably misses
  the motivating incident: slow is not in `is_degraded`, and at ~25%
  intermittent degradation P(3 consecutive) ≈ 1.6% per probe —
  expected first edge after ~21 h, followed by UP churn.
- Event payload carries the rates ("19% error / 12% slow over 24 h"),
  not a bit-flip.
- `health_events` discipline: dedup = "opposite direction OR no prior
  event" (bootstrap-safe); UNIQUE constraint + `INSERT OR IGNORE` in
  one transaction (multiple systemd-timer processes share the SQLite
  file, `Persistent=true` makes them fire together after a resume);
  an incident correlation key so one brownout doesn't fan out as N
  uncorrelated events; subject-retirement events when a ref leaves
  the chains (else a demoted head's stale DOWN suppresses the next
  incident's alert forever).
- **The containment layer becomes an emitter**: first provider-scope
  quarantine, quarantine-TTL escalation, chain fell through to the
  `claude_code` tail / resolved empty. Today the most drastic
  automatic state changes are silent.
- **D1 generalization**: probe whatever each tier's head actually is
  (the provider-agnostic `probe_cell` machinery exists) so a non-NIM
  head no longer amputates detection.

## Wave 3 — autonomous repair (one design, two specs)

- **Propose-PR mode**: when a fresh, guarded regeneration proposes a
  chain change, open a PR via `GhCli.pr_create` (pattern:
  `dev_runner.py:2067`) with an evidence dossier (the PR #76 body as
  template). Guards: onset hysteresis (sustained degradation, e.g.
  24 h) **and** recovery-side agreement (the last k probes must agree
  with the verdict — cells probed during an incident stay "fresh"
  after it resolves); a **proposal fingerprint embargo** (a
  closed-unmerged diff hash is not re-proposed for a cooldown; a
  human `chains.env` commit newer than the evidence window vetoes
  proposing) so a rejected PR doesn't return as a zombie every 6 h;
  at most one open chainpilot PR.
- **Chain invariants validator**, machine-checked in CI and the merge
  gate: every chain ends with `claude_code/<tier>`; thinking-class
  rule for heads; head diversity across tiers (one upstream function
  must not head multiple tiers — note: **current** chains.env
  violates this, glm-5.2 heads opus+haiku, so the validator starts
  warn-only). Protects human edits and chainpilot PRs alike.
- **Reload + breaker reconciliation**: an authenticated cache-clear
  admin endpoint (~20 LOC — `get_settings` is `lru_cache`d and the
  router is rebuilt per request, so no SIGHUP machinery), plus a
  reconciliation pass: clear trips/counters for refs whose chain
  position changed and re-run `probe_seed`. Without it, a merged
  restore-PR re-promoting a previously-quarantined ref is silently
  filtered back out for up to 6 h — the "human role: nothing" loop
  visibly merges a fix that has no effect. Until wave 3, the runbook
  is one command: `systemctl --user restart ferova-llm-proxy`.

## Parking lot — provider-scope quarantine (C1)

Deliberately not scheduled. Panel findings:

- **Dead code as drafted**: both streaming transports convert
  upstream 4xx into in-stream SSE error events
  (`open_router/client.py:236-261`, `openai_compat.py:342-345`), so
  `provider_402`-class reasons never reach `_trip_breaker` — a
  credit-exhausted provider trips one ref at a time as
  `empty_completion`, exactly like today. Prerequisite: plumb the
  upstream status through the peek layer
  (`PeekResult.upstream_error_status`) or re-raise typed errors
  before first yield — a change to the SSE-error contract that needs
  its own careful spec.
- **Marginal benefit is small** once W1.4 alerts on credits days
  earlier: OR-exhausted refs fail in 0.02–0.9 s and the existing
  breaker already quarantines each on first occurrence; C1 saves
  ~7 sub-second hops per hour across all tiers.
- If revisited: **402 only** (401/403 can be transient per-ref;
  404 is model-scoped by nature); the `claude_code` tail is exempt
  from provider-blocking but honors its own ref-trip; an
  all-blocked chain resolves empty and the existing
  `proxy_chain_exhausted` 502 path fires — `Chain.without`'s
  head-resurrection fallback must not silently re-insert a
  quarantined ref.

## Incident replay (v1, honest)

| Failure mode | Wave 1 | Wave 2 | Wave 3 |
|---|---|---|---|
| Week of 19%-error / 12%-slow flapping | Slow-strike demotes the head at runtime between flaps; digest line at next session start ("sonnet: 31% bad over 24 h") | Push alert within the detector window (hours, quantified per spec) | Chainpilot PR after sustained-degradation hysteresis |
| Hard-down head (400) | Existing breaker: 3 strikes → 6-h quarantine within minutes of live traffic; digest shows it | Push alert on DOWN edge | Head-swap PR with evidence; merged + reloaded |
| Credits exhaustion (402) | Threshold line in digest + `/health` days before exhaustion | Push alert at threshold | Sweep skips paid cells; proposals account for dead provider |
| Key revocation (401/403) | Per-ref quarantine when raised pre-stream; streamed errors still land as transient `empty_completion` (known limit until the parking-lot plumbing) | Containment-layer emitter surfaces the escalation | — |
| Non-NIM head promoted | Digest prints `tier unmonitored` | Provider-agnostic head probe closes the gap | Validator + dossier keep monitoring in the loop |

What wave 1 explicitly does **not** do: push alerts (the operator
learns at the next session start, not mid-incident — that is wave 2's
job), autonomous repair (wave 3), provider-wide 402 containment
(parked).

## Revision log (v0 → v1, adversarial panel 2026-07-11)

Three-critic panel (codebase consistency / failure modes /
minimality), findings that changed the design:

1. C1 provider-scope was **unimplementable as drafted** (SSE-error
   path never reaches the classifier) → parked, with plumbing named
   as prerequisite.
2. N-consecutive edge detection **cannot see the motivating
   incident** (slow excluded; 25% flap ≈ 1.6%/probe for 3
   consecutive) → rate-based windowed detector, wave 2.
3. LogNotifier terminus **repeats the incident's surfacing failure**
   (130 unread warnings on incident day) → wave 1 pivots to the
   session-start digest; Notifier port deferred to the wave that
   designs its real consumer.
4. Slow-trip sharing the hard-failure counter → separate k-of-n slow
   counter, no hard-quarantine escalation, budget-retry path covered,
   `PeekResult.final_output_tokens` plumbing named.
5. Unbounded 6-h full-matrix sweep ≈ 1,360 paid requests/day → sweep
   bounded to chain-relevant cells, 429 = observer interference,
   credits-aware skip, inlined into the regen cycle (no new CLI).
6. `chains.env` editable by the Coder → wave-1 blacklist guard.
7. Non-NIM heads unmonitored (`STATUS_SKIPPED`) and repair can
   amputate detection → digest warning now, provider-agnostic probe
   in wave 2.
8. R4 reload without breaker reconciliation makes restore-PRs no-ops
   → reconciliation pass specified; R4 bundled with R3 (its only
   consumer), one-command restart until then.
9. health_events dedup bootstrap/race/retirement defects → wave-2
   requirements recorded before the table exists.

## Open questions (operator)

1. Custom notifier transport (gates wave 2).
2. Slow-strike defaults (`slow_latency_gate_s`, `slow_tps_floor`,
   k-of-n) — to be derived from `nim_health_probe` history at spec
   time; the probe layer's 8-s slow threshold is the natural anchor.
3. Sweep budget: candidate-pool size and per-provider caps (target
   requests/cycle).
4. Digest in `safe_merge.sh` preflight: wanted?
5. Invariants validator: wave 3 as designed, or earlier as a
   standalone warn-only spec (it also protects human edits — and
   would flag today's glm-5.2 double-head)?
