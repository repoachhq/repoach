# systemd --user units

Version-controlled deploy units for the Ferova background cadences (run as
`systemctl --user`, the same pattern as the existing `ferova-nim-health`
timer). Paths use the `%h` specifier (the invoking user's home), so they assume
the checkout at `%h/Documents/work/ferova` and the `ferova` conda env
at `%h/anaconda3/envs/ferova`; adjust `WorkingDirectory` / `ExecStart` if
either differs.

## Chain Autopilot cadence (`ferova-chainpilot`)

Runs one **model-first** `chains.env` regeneration on a timer — fetch the
Artificial Analysis capability ranking, sweep the provider×model matrix, pick
models by capability (Claude-anchored), expand each to its serving providers
(NIM-first, then by probe latency) and render the three `MODEL_*` chains.
**Shadow by default**: the `.service` runs `ferova regenerate-chains` with no
`--apply`, so it computes + logs the chains it *would* write and never touches
`chains.env`. (The earlier mechanical `ferova autopilot` cycle remains
available to run by hand; the cadence now drives the model-first path.)

**Prerequisite**: `FEROVA_ARTIFICIAL_ANALYSIS_API_KEY` must be set on the host
(the AA fetch fails loud without it). It is read from the checkout's `.env`.

Install + enable:

```bash
cp deploy/systemd/ferova-chainpilot.service ~/.config/systemd/user/
cp deploy/systemd/ferova-chainpilot.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ferova-chainpilot.timer
systemctl --user list-timers ferova-chainpilot.timer
```

Inspect what it has been proposing (shadow) in the service journal:

```bash
journalctl --user -u ferova-chainpilot.service -n 50 --no-pager \
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
Environment=FEROVA_CHAINPILOT_APPLY_ENABLED=true
```

```bash
systemctl --user daemon-reload
systemctl --user restart ferova-chainpilot.timer
```

### Tuning the interval

`OnUnitActiveSec=6h` balances cost (a full sweep is ~minutes) against
responsiveness (attribution needs ≥3 probes inside its 24h window before it acts,
so ~4 probes/24h lets a faulting cell be caught within ~12–18h). Lower it for a
twitchier loop, raise it to be cheaper.
