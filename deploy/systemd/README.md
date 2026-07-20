# systemd --user units

Version-controlled deploy units for the Repoach runtime and background
cadences (run as `systemctl --user`). Paths use the `%h` specifier (the
invoking user's home), so they assume the checkout at
`%h/Documents/work/repoach` and its `.venv` runtime (`pip install -e
".[dev]"` inside `%h/Documents/work/repoach/.venv`); adjust
`WorkingDirectory` / `ExecStart` if either differs.

Common prerequisite: the units append their output under the checkout's
`logs/` directory (gitignored) — `mkdir -p ~/Documents/work/repoach/logs`
once before the first install.

## LLM proxy (`repoach-llm-proxy`)

The chain-failover proxy sidecar every factory bot calls. Binds
`127.0.0.1:8084` for now: on a machine where the sharp-agent stack still
owns `127.0.0.1:8082`, the two proxies coexist and repoach's factory must
be pointed at its OWN proxy (repoach's `chains.env` + provider keys)
via the checkout's `.env`:

```
REPOACH_LLM_PROXY_BASE_URL=http://127.0.0.1:8084
```

When the sharp-agent stack is retired, drop the `.env` override and the
`REPOACH_PROXY_PORT=8084` line in the unit to reclaim the default 8082.

```bash
mkdir -p ~/Documents/work/repoach/logs
cp deploy/systemd/repoach-llm-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now repoach-llm-proxy.service
curl -s http://127.0.0.1:8084/health
```

## NIM chain-head health probe (`repoach-nim-health`)

Runs `repoach monitor-chains` every 15 minutes, persisting each sweep to
the `nim_health_probe` table (`REPOACH_DB_PATH`). This history seeds the
proxy's health breaker at startup and feeds Chain Autopilot attribution
(which needs ≥3 probes inside its 24h window before it acts) — without
the cadence both start blind.

```bash
cp deploy/systemd/repoach-nim-health.service ~/.config/systemd/user/
cp deploy/systemd/repoach-nim-health.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now repoach-nim-health.timer
systemctl --user list-timers repoach-nim-health.timer
```

## Chain Autopilot cadence (`repoach-chainpilot`)

Runs one **model-first** `chains.env` regeneration on a timer — fetch the
Artificial Analysis capability ranking, sweep the provider×model matrix, pick
models by capability (Claude-anchored), expand each to its serving providers
(NIM-first, then by probe latency) and render the three `MODEL_*` chains.
**Shadow by default**: the `.service` runs `repoach regenerate-chains` with no
`--apply`, so it computes + logs the chains it *would* write and never touches
`chains.env`. (The earlier mechanical `repoach autopilot` cycle remains
available to run by hand; the cadence now drives the model-first path.)

**Prerequisite**: `REPOACH_ARTIFICIAL_ANALYSIS_API_KEY` must be set on the host
(the AA fetch fails loud without it). It is read from the checkout's `.env`.

Install + enable:

```bash
cp deploy/systemd/repoach-chainpilot.service ~/.config/systemd/user/
cp deploy/systemd/repoach-chainpilot.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now repoach-chainpilot.timer
systemctl --user list-timers repoach-chainpilot.timer
```

Inspect what it has been proposing (shadow) in the service journal:

```bash
journalctl --user -u repoach-chainpilot.service -n 50 --no-pager \
  | grep -E "mfc_servable_index|mfc_select|mfc_regenerate|mfc_expand_model_dropped"
```

`mfc_regenerate` reports `changed`/`written` and the per-tier entry counts;
`mfc_servable_index` reports `unmatched_cells` (equivalence-table coverage).

### Arming the write (deliberate)

Leave it shadow for a while and review `chain_mutation_log` first. To let the
cadence actually rewrite `chains.env` (atomic write + `.bak`), add the flag to the
service and reload:

```ini
[Service]
Environment=REPOACH_CHAINPILOT_APPLY_ENABLED=true
```

```bash
systemctl --user daemon-reload
systemctl --user restart repoach-chainpilot.timer
```

### Tuning the interval

`OnUnitActiveSec=6h` balances cost (a full sweep is ~minutes) against
responsiveness (attribution needs ≥3 probes inside its 24h window before it acts,
so ~4 probes/24h lets a faulting cell be caught within ~12–18h). Lower it for a
twitchier loop, raise it to be cheaper.
