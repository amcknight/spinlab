# Segments-v07 data collection playbook

Run-by-run steps for building the permanent v07 validation corpus. The
corpus is the rows in the `attempts` table (a.k.a. `event_attempts`); the
`segment_fits` table is derived and can always be rebuilt with
`spinlab fit rebuild`.

The plan is to escalate: a short sanity session on a known game first,
then a long multi-game session that produces the permanent dataset.
Between each step, inspect with `spinlab fit inventory` to confirm the
pipeline did what you expected.

## Tools you'll use

| Command | Purpose |
|---|---|
| `spinlab fit inventory --game G` | One-screen summary: segments, events by outcome and source, fittable count, fit count, latest fit time. The first thing to run after any session. |
| `spinlab fit inventory --all` | Same, across every game in the DB. Use for the multi-game session. |
| `spinlab fit list --game G` | Tab-separated row per fittable segment. Useful for picking interesting ids to drill into. |
| `spinlab fit show <segment_id>` | Pretty-print the latest v1 envelope for a segment. |
| `spinlab fit show <id> --history N` | One-line summary per recent fit (newest first). |
| `spinlab fit rebuild --game G` | Wipe and cold-refit every `segment_fit` row for a game. Run after a speedrun or to revalidate against a new model version. |
| `spinlab fit-pool --game G` | EB pool refit across the game's segments (need n>=5 on >=2 segments). |
| `spinlab db reset` | Nuke the entire DB. Use before the long-run session. |

All `--config` flags walk up parent directories looking for `config.yaml`,
so you can run any of these from anywhere under the repo without `cd`.

## Coverage gaps to know about

Live model updates fire only from **practice mode**. Speedrun and
reference-finalize attempts land in `attempts` correctly but don't
trigger refits live. The way to catch up after a non-practice session
is `spinlab fit rebuild --game G`.

Reference runs produce one `survived` event per segment (n=1), which
is below the n>=5 fittable floor; expect those to show up in the
inventory but never trigger a fit.

## Pre-flight (one-time)

1. **Confirm `[fits]` is installed.** When the dashboard boots, the log
   line `segments-v07 silent fit pipeline disabled — [fits] extra not
   installed` should NOT appear. If it does:
   ```powershell
   pip install -e '.[fits]'
   ```
2. **Backup the current DB.** Even if empty, snapshot it so any future
   migration mistake is reversible:
   ```powershell
   copy data\spinlab.db data\spinlab.db.preflight-backup
   ```
3. **Run baseline tests** — full suite (`python -m pytest`) should be
   green. Don't start collecting data over a red baseline.

## Step 1 — Short reference run on a known game

Goal: confirm segment recording produces the rows you expect; learn
nothing about the model yet.

1. Start the dashboard. Watch for `JAX prewarm complete` in the log.
2. Do a reference run of 1 level (2 segments) on a known game.
3. Finalize the run.
4. Run inventory:
   ```powershell
   spinlab fit inventory --game <id>
   ```

Expected output:
- `Segments: 2 (2 active)`
- `Event attempts: 2 across 2 segments (2 survived, 0 died)`
- `by source: reference: 2 events (2 segments)`
- `Fittable (n>=5): 0 segments — need >= 5 events on a segment to trigger a fit`
- `Fits stored: 0 segment_fit, 0 pool_fit`

If `Segments` is wrong, the reference recorder didn't capture what you
expected. If `Event attempts` is 0 with the segments populated, the
finalize step didn't write rows.

## Step 2 — Practice attempts to validate the v07 path

Goal: confirm event_attempts populate from practice mode and that fits
fire once a segment crosses n>=5.

1. Practice the segments from Step 1. Get at least 5–8 attempts on
   each (mix of deaths and clears so the model has both signals).
2. Run inventory:
   ```powershell
   spinlab fit inventory --game <id>
   ```

Expected:
- `Event attempts` total > 5 with both `survived` and `died` populated
- `by source: practice: N events`
- `Fittable (n>=5)` lists the segments you practiced
- `Fits stored: M segment_fit` with M >= number of attempts past the floor

Then pretty-print one segment's fit:
```powershell
spinlab fit show <segment_id>
```

Sanity-check the result block:
- `Status: converged: yes, fittable: yes, band_source: laplace` (or `nuts`)
- `Derived: M_clear median: <ms>` — should be close to your actual
  practice times on that segment
- `death_rate_next: <pct>%` — should be plausible vs how often you died

If `fittable: no` or `converged: no`, that's a real signal — the model
genuinely struggled with those events. Note it, don't fix it.

## Step 3 — Reset and start the permanent collection

Goal: build the multi-game permanent dataset. Everything from now on
should be preserved.

1. **Backup the current DB:**
   ```powershell
   copy data\spinlab.db data\spinlab.db.pre-collection
   ```
2. **Reset:**
   ```powershell
   spinlab db reset
   ```
3. **Backup the empty post-reset state** as a known baseline:
   ```powershell
   copy data\spinlab.db data\spinlab.db.collection-baseline
   ```

## Step 4 — Long multi-game run

Goal: build the permanent corpus. Drive every play mode against every
game you want represented.

For each game:
1. Reference run (defines segments).
2. Some speedrun-mode passes (cold attempts from start).
3. Some practice (drives the live v07 refit pipeline).

You don't need to run a multi-session reference — single-session is
sufficient for the corpus.

## Step 5 — Post-session catch-up

Speedrun and reference attempts land in `attempts` but don't trigger
live refits. After the long session:

1. **Inventory every game:**
   ```powershell
   spinlab fit inventory --all
   ```
   Confirm every game shows expected event totals AND non-zero
   `speed_run` and `practice` rows. If `speed_run: 0 events` appears
   for a game you speedran, the wiring is broken.

2. **Cold-refit each game** (picks up speedrun events the live path
   missed):
   ```powershell
   spinlab fit rebuild --game <id>
   ```
   One line per fit with wall time and convergence status.

3. **Pool fit each game with >=2 fittable segments:**
   ```powershell
   spinlab fit-pool --game <id>
   ```

4. **Final inventory snapshot** — confirms fits are present and
   reflects the post-rebuild state:
   ```powershell
   spinlab fit inventory --all
   spinlab fit inventory --all --json > collection-inventory-YYYY-MM-DD.json
   ```

## Step 6 — Permanent backup

The dataset is in `data/spinlab.db`. Snapshot it as the named corpus:

```powershell
copy data\spinlab.db data\spinlab-collection-YYYY-MM-DD.db
```

The `segment_fits` table is regenerable; the `attempts` table is not.
If disk pressure ever forces a trim, the JSON inventory + the `attempts`
table are what to preserve.

## Revalidating against a future model

When a new v07 (or v08, etc.) lands:

```powershell
copy data\spinlab.db data\spinlab.db.pre-revalidate
spinlab fit rebuild --game <id>
spinlab fit-pool --game <id>
spinlab fit inventory --game <id>
```

Diff inventory output against the prior snapshot (the JSON file from
Step 5) to see what the new model changes. The `attempts` rows are
identical between runs; only the fit-derived columns differ.

## Quick failure-mode reference

| Symptom | Likely cause |
|---|---|
| `Fits stored: 0` after practicing past n=5 | `[fits]` extra not installed; check dashboard startup log for warning |
| `Fits stored: 0` after speedrun only | Expected — speedrun doesn't trigger live refits. Run `spinlab fit rebuild` |
| `Event attempts: 0` after a reference run | Reference finalize didn't write rows. Check `spinlab` logs |
| `speed_run: 0 events` after a speedrun session | Speedrun wiring isn't producing attempts; check `speed_run.py`/`SpeedRunSession._record_attempt` path |
| `fittable: no` on most segments | PPC tension or non-convergence; the model genuinely struggled. Read individual `fit show` outputs to see which caveat fired |
| `fittable: no` AND `converged: no` | The fit didn't find a posterior. Often happens with all-died or all-survived event sequences. Get more diverse attempts |
| Dashboard hitches multi-second mid-attempt | NUTS fallback on the request path. Known issue — Laplace covariance failed PD; the prototype falls back to NUTS sampling which takes 1–10s |
