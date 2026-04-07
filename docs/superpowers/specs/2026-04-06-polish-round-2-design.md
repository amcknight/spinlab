# Polish Round 2 — Design Spec

Five independent improvements from the 2026-04-06 testing session.

## 1. Tighter log format

**Current** format string in `python/spinlab/cli.py` `_setup_file_logging()`:

```
2026-04-06 19:20:26,266 INFO spinlab.practice — practice: started session=...
```

**New**:

```
04-06 19:20:26 INFO practice — practice: started session=...
```

Changes to the `Formatter` call:
- Custom `datefmt="%m-%d %H:%M:%S"` — drops year and milliseconds.
- Strip `spinlab.` prefix from `%(name)s`. Use a logging `Filter` that rewrites `record.name` (e.g. `record.name.removeprefix("spinlab.")`), or switch to a custom `Formatter` subclass that does the same in `format()`.
- Keep message-level prefixes (`practice:`, `capture:`, `cold_fill:`, etc.) — they're short, useful for grep, and already consistent.

## 2. Fix Vite proxy target

`frontend/vite.config.ts` proxies `/api` to `http://localhost:8000`, but the dashboard serves on port **15483** (configured in `config.yaml` → `network.dashboard_port`).

**Fix**: Change proxy target to `http://localhost:15483`.

During development: run `spinlab dashboard` (CAW) + `npm run dev`, then use `http://localhost:5173`. Hot reload, correct API proxy, no rebuild step.

## 3. Debounced live sliders (remove Apply button)

**Goal**: Tuning sliders update Expected/Trend columns live as you drag, removing the need for an Apply button.

**Implementation** in `frontend/src/model.ts`:
- Add a module-level debounce timer (200ms).
- On each slider's `input` event, reset the timer. When it fires, call `applyTuningParams()` (which POSTs params and refreshes the model table).
- The number input's `input` event does the same (sliders and number inputs are already synced two-way).
- Remove the Apply button from `frontend/index.html` and its event listener from `model.ts`.
- Keep Reset Defaults — it sets slider values programmatically, which triggers the `input` events, which triggers the debounced apply. Remove the explicit `applyTuningParams()` call from `resetTuningDefaults()` to avoid a double-fire.

## 4. Log Clear All Data

**In** `python/spinlab/routes/system.py`, the `POST /api/reset` endpoint:

Add `logger.warning("reset: clearing all data for game=%s", gid)` before `db.reset_game_data(gid)`. Warning level — it's destructive and worth noticing in the log.

Requires adding `logger = logging.getLogger(__name__)` at the top of the file if not already present.

## 5. Python-side error logging for API errors

Ensure Python logs its own errors before returning 4xx/5xx responses, so they appear in `spinlab.log`.

Key endpoints to audit:
- `POST /api/estimator-params` (`routes/model.py`) — log validation failures (unknown param names, out-of-range values).
- `GET /api/segments`, `PATCH /api/segments/{id}` (`routes/segments.py`) — log fetch/patch failures.
- `POST /api/reset` (`routes/system.py`) — covered by item 4.
- Any other endpoint returning an error response.

Pattern: `logger.warning("endpoint_name: description, key=%s", value)` before raising `HTTPException` or returning error JSON.

"Failed to fetch" errors (network-level, server unreachable) cannot be logged server-side — the request never reaches Python. These are addressed by item 2 (correct proxy target) and general infra reliability.
