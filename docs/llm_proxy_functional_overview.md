# LLM proxy — functional overview

> Status: **living document** (started 2026-07-12). A plain-language,
> block-by-block walkthrough of how the proxy serves one request, plus
> the elements that support it. It grows as we detail each block —
> expect sections to deepen over time. Diagram:
> `llm_proxy_functional_overview.svg` (+ `.png`). For implementation
> detail see `proxy_routing_redesign_architecture.md` and the code
> under `src/ferova/llm_proxy/`.

## What the proxy is

An Anthropic-compatible gateway that fronts several LLM providers. A
caller does **not** ask for a specific model — it asks for a
**capability tier** (opus / sonnet / haiku) and the proxy chooses which
concrete model answers, walking an ordered failover chain and stepping
over anything that recently failed. Agents run at near-zero cost with a
guaranteed quality floor (a local Claude is the tail of every chain).

![Functional diagram](llm_proxy_functional_overview.png)

## How a request flows

The top row is the request coming in; it drops down to the providers on
the right and comes back along the second row as the response is
handled. Follow the diagram left-to-right, then down, then back left.

### 1. CLIENT

The callers are **programs** — agents, tools, the CLI — not humans
typing. Each request carries three things:

- a **capability tier** (the "intelligence level": opus / sonnet /
  haiku) — *not* a specific model;
- the **context** — everything the model must read: instructions, the
  task itself, and any supporting material (documents, code, history);
- an **authentication token** — a shared secret, attached to the
  request envelope (a header), not to its body.

This block is both the entry and the exit: the whole journey starts and
ends here.

### 2. AUTHENTICATE

The door check. It reads only the **token on the envelope** — never the
body — and compares it against the configured secret
(`FEROVA_ANTHROPIC_AUTH_TOKEN`). Valid → the request continues; missing
or wrong → rejected immediately with `401`, before reaching any model.
If no secret is configured, the door is open (no-op). It is a simple
shared secret because the proxy is a private, local sidecar, not an
internet-facing service.

Code: `require_api_key` in `src/ferova/llm_proxy/api/dependencies.py`,
wired per-route via `Depends(require_api_key)` in `api/routes.py`
(opt-in, which is why `GET /health` deliberately omits it).

### 3. RESOLVE CHAIN

Translates the requested tier into a concrete plan: read the alias the
client sent → map it to a tier (opus / sonnet / haiku) → look up that
tier's **chain**: an *ordered* list of `(provider, model)` candidates,
preferred first (free NIM) down to the last resort (local Claude). The
chain comes from `CONFIGURATION` (loaded at startup); this block reads
the order, it does not decide it.

### 4. FILTER

Removes candidates the circuit breaker currently has **on cooldown**
(dead, erroring, out of credits, bad key, rate-limited). It is a
read-only consumer of breaker state — it applies penalties, it never
sets them. Note: a **slow** candidate is *not* filtered today (see
Known limits #2).

### 5. CALL

The first contact with the outside world: take the top surviving
candidate, call **its provider** using that provider's access key (the
`provider credentials` from configuration — the proxy is now itself a
client, authenticating to the provider), in **streaming** mode. Two
outcomes: a response stream begins → on to `ANALYZE`; or the call
raises before any content (connection, auth, outage, timeout) → the
amber `call error` arrow straight to `ARBITRATE`. Only one candidate is
called at a time.

### 6. ANALYZE

Reads the streamed response **in full and buffers it before deciding
anything**, then classifies it: **text** (real content) or **empty**.
Why read it whole? The dominant failure mode of open-weight models is
the *empty* response — the model spends its whole budget "thinking"
silently and returns nothing, as an HTTP-200 success. Buffering lets
the proxy catch that and switch candidates *before the client sees
anything*. The price is no fast first byte: perceived latency equals
full-completion time (a deliberate trade-off; see Known limits #4).

### 7. ARBITRATE

The decision hub. Given the verdict (or a call error), it chooses:

- **text** → `SERVE` the response, and clear the candidate's penalty
  in the breaker (the "success" half of feedback ①);
- **empty, looks budget-starved** → one **budget retry** on the same
  candidate; if it now has content, serve;
- **empty (hopeless) or error** → penalize the candidate (the
  "failure" half of ①) and move to the **next candidate** (the grey
  loop back to `CALL`);
- **no candidate left** → **total failure**: an error is returned to
  the client (the red arrow).

This is where both loops originate: the grey "next candidate" loop
(within one request) and the cyan breaker-feedback loop (across
requests).

### 8. SERVE

The exit door. Sends the **buffered** response back to the client (as a
stream, though it was fully collected first) and **reports the real
model** that answered — closing the abstraction: the client asked for a
tier, gets a concrete answer, and is told which model produced it.

## The circuit breaker and its feedback loop

The breaker is the system's memory of what is currently unhealthy. It
tracks penalties **per `(provider, model)` cell** — never per whole
provider (one provider can have one dead model and others perfectly
healthy). Penalty length depends on the fault class: **2 min** for a
transient blip (timeout, 5xx, rate-limit, empty), **6 h** for an
account-level fault that will not self-heal (bad key, no credits,
forbidden, bad id), **7 d** for a model the provider retired. Three
consecutive failures escalate to the long penalty; a successful,
content-bearing response clears the slate.

The **feedback loop** is two cyan arrows:

- **①** `ARBITRATE → breaker`: the call's outcome penalizes (on
  failure) or rehabilitates (on success) the model.
- **②** `breaker → FILTER`: the state is re-read by the *next request's*
  filtering, which drops the penalized model.

The loop therefore operates **between requests**, not within one:
within a single request the chain is filtered once, then walked; a
penalty written mid-request takes effect at the next request's filter.

State is in-process (lost on restart) and rebuilt at startup by the
health probe's history (`pre-fills the breaker`). It is also exposed,
unauthenticated, for inspection (`state exposed`).

## Supporting elements

- **CONFIGURATION** — the chains (which models, in what order, per
  tier) and the provider access keys, read at startup. Feeds
  `RESOLVE CHAIN` (chains), `CALL` (credentials) and the health probe
  (which heads to test).
- **PROVIDERS** — NIM (free, heads most chains), DeepSeek / Kimi
  (direct), Cerebras / Groq, OpenRouter (aggregator), and local Claude
  as the tail / last resort of every chain.
- **HEALTH PROBE** — an off-path job (every 15 min) that calls each
  tier's chain head directly, bypassing the request path, and persists
  a health history. That history pre-fills the breaker at startup so it
  does not start cold.

*(These four are summarised here and will get their own detailed
sections as we go.)*

## Known limits

Faults we know about; some have a prepared fix, some are deferred, some
are accepted trade-offs.

1. **Ambiguous empty (ANALYZE)** — an upstream error that occurs
   *mid-stream* is folded into the stream and looks like an *empty*
   response; its real cause (e.g. credits) is lost before the breaker.
   *Status: deferred — needs the upstream status surfaced through the
   read layer.*
2. **Slow is not penalized (BREAKER)** — a slow-but-successful response
   (12–15 s) rehabilitates the model instead of penalizing it, so a
   sick head re-promotes itself endlessly. *Status: fix prepared — a
   too-slow response will count as a failure; shipped in observation
   mode first.*
3. **Credit exhaustion unmonitored (HEALTH PROBE)** — a provider
   running out of credits is not tracked; discovered mid-incident.
   *Status: fix prepared — balance monitoring with a floor alert.*
4. **No fast first byte (ANALYZE)** — full-read buffering means
   perceived latency is full-completion time. *Status: accepted
   trade-off (the price of catching empty responses).*
5. **Config read only at startup (CONFIGURATION)** — applying a config
   change requires restarting the service. *Status: later — hot reload
   + breaker reconciliation.*
6. **Head resurrection (FILTER)** — if every candidate is on cooldown,
   the preferred one is retried anyway: an implicit "serve degraded"
   valve, never formally decided. *Status: undecided.*

## To be expanded

- Circuit breaker internals (TTL escalation, probe seeding).
- The health probe and the offline chain-repair path.
- The `/v1/agent` capability-gateway endpoint (native tool use).
- Provider transports and the SSE-error folding behind limit #1.
