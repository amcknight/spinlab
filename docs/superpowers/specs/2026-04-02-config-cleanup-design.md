# Config Cleanup: Named Constants, Estimator Params, and Dashboard Tuning

## Summary

Factor out magic numbers across the codebase into named constants, add a tunable-params system to estimators, and expose Kalman filter knobs in the dashboard for live experimentation with instant rebuild.

Three workstreams:
1. **Estimator params plumbing** — ABC method, DB storage, rebuild wiring
2. **Dashboard UI** — collapsible tuning panel in Model tab
3. **Code hygiene sweep** — name all magic numbers, delete dead cosmetic logic

## 1. Estimator Params Plumbing

### ParamDef

New dataclass in `spinlab/estimators/__init__.py`:

```python
@dataclass
class ParamDef:
    name: str           # internal key, e.g. "D0"
    display_name: str   # UI label, e.g. "Initial Drift"
    default: float
    min_val: float
    max_val: float
    step: float
    description: str
```

All params are floats for now. When the first bool/dropdown/select param is needed, add a `type` field then. Not before.

### Estimator ABC Changes

Two additions to `Estimator`:

```python
class Estimator(ABC):
    def declared_params(self) -> list[ParamDef]:
        """Tunable params with metadata. Default: no params."""
        return []

    @abstractmethod
    def rebuild_state(self, attempts: list[AttemptRecord], params: dict | None = None) -> EstimatorState:
        """Rebuild from scratch. If params provided, override defaults."""
        ...
```

`init_state` and `process_attempt` also get an optional `params` argument so new segments and ongoing processing pick up the current tuning (e.g. R_blend during R re-estimation):

```python
def init_state(self, first_attempt: AttemptRecord, priors: dict, params: dict | None = None) -> EstimatorState:

def process_attempt(self, state: EstimatorState, new_attempt: AttemptRecord,
                    all_attempts: list[AttemptRecord], params: dict | None = None) -> EstimatorState:
```

### Kalman Declared Params

| Param | Display Name | Default | Min | Max | Step | Description |
|-------|-------------|---------|-----|-----|------|-------------|
| `D0` | Initial Drift | 0.0 | -5.0 | 5.0 | 0.1 | Assumed improvement rate before data (seconds/attempt). 0 = no assumption. |
| `R` | Obs. Noise | 25.0 | 0.01 | 1000.0 | 0.1 | How noisy individual attempts are. Higher = smoother, slower to react. |
| `P_D0` | Drift Variance | 1.0 | 0.01 | 50.0 | 0.1 | Initial uncertainty about drift. Higher = more willing to learn drift from data. |
| `Q_mm` | Process Noise (Mean) | 0.1 | 0.001 | 10.0 | 0.01 | How fast true skill is expected to change. Higher = more reactive. |
| `Q_dd` | Process Noise (Drift) | 0.01 | 0.001 | 5.0 | 0.001 | How fast drift itself changes. Higher = trend estimates shift faster. |
| `R_floor` | Noise Floor | 1.0 | 0.01 | 10.0 | 0.01 | Minimum observation noise. Prevents filter from over-trusting single attempts. |
| `R_blend` | R Learning Rate | 0.3 | 0.01 | 1.0 | 0.01 | How fast observation noise adapts. 1.0 = fully trust new estimate. |

Ranges are generous "won't crash the math" bounds, not correctness claims.

### ExpDecay and RollingMean

`declared_params()` returns `[]` for now. Future knobs (window function for RollingMean, etc.) added by implementing the method.

### Behavioral Changes

- **DEFAULT_D changes from -0.5 to 0.0** — honest no-assumption prior.
- **R_REESTIMATE_INTERVAL deleted.** R re-estimates on every completed attempt (the R blending already provides smoothing, the interval was redundant inertia).

### Storage

New row in existing `allocator_config` table: key `"estimator_params:{estimator_name}"`, value is JSON of the params dict. Same storage pattern as allocator weights.

Example: `("estimator_params:kalman", '{"D0": 0.0, "R": 25.0, ...}')`

If no row exists, use `declared_params()` defaults.

### Rebuild Flow

1. User changes param → POST `/api/estimator-params`
2. Endpoint saves to DB
3. Calls `scheduler.rebuild_all_states()` (already exists)
4. `rebuild_all_states` loads params from DB, passes to each estimator's `rebuild_state(attempts, params=params)`
5. Each estimator replays all attempts with new params, saves updated state + model output
6. SSE broadcasts new state → dashboard model table updates

This is pure arithmetic per segment (no I/O during replay), so even hundreds of attempts across dozens of segments will rebuild in under a millisecond.

## 2. Dashboard UI

### Tuning Panel

Location: Model tab, collapsible section below the estimator dropdown.

Behavior:
- Shows params for the **currently selected** estimator only. Switching estimator repopulates the panel.
- Each param renders as: display name label, range slider, numeric input (synced to slider).
- **Not auto-applied.** An "Apply" button sends all current slider values as a single POST. This prevents accidental rebuilds from dragging a slider.
- "Reset Defaults" link restores declared defaults and triggers rebuild.
- If an estimator has no declared params, panel shows "No tunable parameters" — present but empty, so the user knows the panel exists for other estimators.

### New API Endpoints

**`GET /api/estimator-params`**

Returns the current estimator's param schema and values:

```json
{
  "estimator": "kalman",
  "params": [
    {"name": "D0", "display_name": "Initial Drift", "value": 0.0, "default": 0.0, "min": -5.0, "max": 5.0, "step": 0.1, "description": "..."},
    ...
  ]
}
```

Frontend uses this to render sliders with correct ranges. Values come from DB if saved, otherwise from declared defaults.

**`POST /api/estimator-params`**

Body: `{"params": {"D0": 0.0, "R": 25.0, ...}}`

Saves to DB, triggers `rebuild_all_states`, returns success. Model table update arrives via SSE.

### Styling

Matches existing dark theme. Collapsible section uses same pattern as other Model tab sections. Sliders styled with existing CSS variables.

## 3. Code Hygiene Sweep

### Delete

- **Drift threshold ±10** in `model.js:139-140, 185-186` — remove improving/regressing/flat arrow logic and CSS classes. The raw `ms_per_attempt` number already displays; qualitative labels add nothing.
- **Uncertainty threshold and confidence labels** in `kalman.py:177-182` — remove the `"confident"/"moderate"/"uncertain"` labeling from `drift_info()`. Premature until models are actually validated.
- **`R_REESTIMATE_INTERVAL`** — delete the constant, inline R re-estimation to every completed attempt.

### Name and Promote to File-Level Constants

These remain in-code (not config.yaml, not dashboard), but get proper names at the top of their files instead of being buried as raw literals.

**Python timing/connection (`dashboard.py`, `cli.py`, `practice.py`, `session_manager.py`):**
- `TCP_CONNECT_TIMEOUT_S = 2`
- `TCP_RETRY_DELAY_S = 2`
- `TCP_EVENT_TIMEOUT_S = 1.0`
- `SSE_KEEPALIVE_S = 30`
- `PRACTICE_STOP_TIMEOUT_S = 5`
- `SEGMENT_LOAD_TIMEOUT_S = 1.0`
- `SOCKET_CONNECT_TIMEOUT_S = 2` (cli.py lua-cmd)

**Python query/UI limits:**
- `RECENT_ATTEMPTS_LIMIT = 8` (session_manager.py)
- `SESSION_HISTORY_LIMIT = 10` (db/sessions.py)
- `SSE_QUEUE_MAX = 16` (sse.py)
- `RECENT_ATTEMPTS_DB_LIMIT = 8` (db/attempts.py)

**JavaScript (`model.js`, `api.js`):**
- `TOAST_TIMEOUT_MS = 8000`
- `FALLBACK_POLL_MS = 5000`

**Lua (`spinlab.lua`, `poke_engine.lua`):**
- `MAX_RECORDING_FRAMES` already named (line 26) — good
- `HEARTBEAT_INTERVAL_FRAMES = 60` (currently unnamed at line 845)
- `POKE_SETTLE_FRAMES = 30` (poke_engine.lua, already named at line 44)
- `REPLAY_PROGRESS_INTERVAL_MS = 100`
- `CHAR_W = 6` (already named at line 315)
- `AUTO_ADVANCE_DEFAULT_MS = 2000`

### Already Fine

- Spinrec format constants (already named: `MAGIC`, `VERSION`, `HEADER_SIZE`)
- Memory addresses (documented at top of lua files per CLAUDE.md)
- ROM extension whitelist (appropriate to hardcode)
- Input bitmask encoding (SNES hardware spec)
- `MIN_POINTS_FOR_FIT = 3` (mathematical constraint, already named)
- Allocator weight sum = 100 (validation constraint)

## Out of Scope

- Per-estimator clean vs total param split (option 1: single param set, estimator applies internally)
- RollingMean window function knobs (future: implement `declared_params()` when ready)
- ExpDecay tunable params (curve_fit initial conditions are not meaningfully tunable)
- Settings tab (not enough settings to justify a third tab yet)
- config.yaml expansion (these are live-tuning knobs, not startup config)
