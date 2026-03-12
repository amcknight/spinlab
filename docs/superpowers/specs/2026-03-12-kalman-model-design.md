# SpinLab Kalman Model & Allocator Redesign — Design Spec

## Goal

Replace the SM-2 spaced-repetition scheduler with a Kalman-filter-based estimation model and pluggable allocator system. Drop button-based Anki ratings in favor of auto-advance on completion. Add a Model tab to the dashboard for full model transparency. Clean break — fresh data start.

## Why

SM-2 requires subjective difficulty ratings and uses review intervals designed for flashcards, not speedrun practice. The Kalman model learns directly from completion times, estimates improvement drift per split, and derives a marginal return signal that allocators can use to prioritize practice where the runner is actually improving most.

## Architecture: Estimator + Allocator Separation

Two pluggable layers replace the monolithic SM-2 scheduler:

1. **Estimator** — observes attempt times, maintains per-split model state, outputs derived signals (expected time, drift, confidence, marginal return). Kalman is the primary implementation.
2. **Allocator** — given all active splits with their estimator state, picks which split to practice next. Multiple strategies available (Greedy, Random, Round Robin).

A thin **coordinator** (`scheduler.py`) holds one estimator + one allocator, loads/saves state from DB, and exposes `pick_next()`, `process_attempt()`, `peek_next_n()` to the orchestrator. Same interface surface as before, different internals.

Both estimator and allocator are switchable via config.yaml (default) and dashboard dropdown (mid-session override).

---

## Estimator Module

### File Structure

```
python/spinlab/estimators/
├── __init__.py       # Estimator ABC + registry
└── kalman.py         # KalmanEstimator + KalmanState
```

### Estimator ABC

```python
class Estimator(ABC):
    name: str

    def init_state(self, first_time: float, priors: dict) -> EstimatorState
    def process_attempt(self, state: EstimatorState, observed_time: float | None) -> EstimatorState
    def marginal_return(self, state: EstimatorState) -> float
    def drift_info(self, state: EstimatorState) -> dict  # drift, confidence, label for dashboard
    def get_population_priors(self, all_states: list[EstimatorState]) -> dict
    def rebuild_state(self, attempts: list[float]) -> EstimatorState  # rebuild from raw data
```

Each estimator defines its own state dataclass. State is serialized as JSON for DB storage.

**Incomplete attempts (death/abort):** `process_attempt` receives `observed_time=None` when `completed=false`. The estimator skips the predict/update cycle (no meaningful observation to incorporate). The attempt is logged in the `attempts` table regardless. State tracks two counters: `n_completed` (completed attempts, used for R re-estimation and prior blending) and `n_attempts` (total including incomplete, for display). `rebuild_state` replays all attempts in order — completed ones get predict/update, incomplete ones are skipped but counted — matching live behavior exactly.

### KalmanEstimator

Implements the full model from `practice-optimizer-model-spec.md`:

**Per-split state (`KalmanState`):**
- μ (expected time), d (drift, seconds/run, negative = improving)
- P (2x2 covariance matrix, stored as 4 floats: P_μμ, P_μd, P_dμ, P_dd)
- R (observation noise variance), Q (2x2 process noise matrix)
- gold (best observed time), n_completed (completed attempts), n_attempts (total including incomplete)

**Operations:**
- `predict(state)` — F @ x, F @ P @ F^T + Q (propagate one step)
- `update(state, observed_time)` — innovation, Kalman gain, state/covariance update
- `process_attempt(state, time)` — predict + update, update gold, increment n_runs
- `marginal_return(state)` — `m_i = -d_i / μ_i`
- `drift_info(state)` — drift value, ±1.96 * sqrt(P_dd) confidence interval, text label
- R re-estimation when `n_completed % 10 == 0` and `n_completed >= 10`, via innovation variance (the math spec suggests ≥5 runs; we use 10 for more stable estimates)
- `rebuild_state(attempts)` — replay all attempts through predict/update to reconstruct state

All 2x2 matrix ops expanded to scalar arithmetic. No numpy dependency. Every function is pure — fully unit-testable.

**Initialization (first run):**
- μ = first observed time
- d = population mean drift (or -0.5 default)
- P = [[R_prior, 0], [0, P_d0]] where P_d0 = 1.0
- R = population mean R (or 25.0 default)
- Q = [[0.1, 0], [0, 0.01]]

**Hierarchical priors:** For all splits, blend local and population priors with `weight = min(1, n_runs / 20)`. Population priors (mean R, Q, d) are computed from splits with ≥ 10 runs. When no splits meet the threshold, `get_population_priors` returns hardcoded defaults (d=-0.5, R=25.0, Q=[[0.1,0],[0,0.01]]).

---

## Allocator Module

### File Structure

```
python/spinlab/allocators/
├── __init__.py        # Allocator ABC + registry
├── greedy.py          # Highest marginal return
├── random.py          # Uniform random
└── round_robin.py     # Cycle through all active splits
```

### Allocator ABC

```python
class Allocator(ABC):
    name: str

    def pick_next(self, split_states: list[SplitWithModel]) -> str | None  # returns split_id or None if empty
    def peek_next_n(self, split_states: list[SplitWithModel], n: int) -> list[str]
```

`SplitWithModel` is a dataclass combining split metadata with estimator output:

```python
@dataclass
class SplitWithModel:
    # Split metadata (from splits table, nullability matches schema)
    split_id: str
    game_id: str
    level_number: int
    room_id: int | None
    goal: str
    description: str
    strat_version: int
    reference_time_ms: int | None
    state_path: str | None
    active: bool
    # Estimator output
    estimator_state: EstimatorState | None  # None if no attempts yet
    marginal_return: float
    drift_info: dict  # {drift, confidence_interval, label}
    n_completed: int
    n_attempts: int
    gold_ms: int | None
```

Allocators receive rich context — future allocators can group, filter, or weigh by any field.

### Implementations

- **Greedy:** Sort by marginal return descending, pick top. Peek returns top N.
- **Random:** Uniform random from active splits. Peek returns N random (without replacement).
- **Round Robin:** Cycle through all active splits in a stable order. Peek returns next N in cycle.

Additional allocators (Softmax, Weighted Average, diffusion variants) can be added later by implementing the ABC. The registry auto-discovers them.

### Design Note: No Lock-in

The allocator receives `SplitWithModel` objects, not just numbers. It doesn't assume linear ordering, independence between splits, or one-way-to-play-a-section. Future allocators can implement grouping (multi-route), exclusion (strat switching), dependency logic (VoI across route choices), or any other sophistication internally without changing the interface.

---

## Database Schema Changes

### Dropped

- `schedule` table (SM-2 state) — deleted entirely
- All existing data — fresh start (optionally keep `attempts` for historical reference, prompt at migration time)

**Migration:** `db.py`'s `_init_schema` detects the old `schedule` table on startup, drops it, creates `model_state` and `allocator_config`, and makes `attempts.rating` nullable. This is automatic — no separate migration script. Running new code against an old DB is safe.

### New: `model_state` table

| Column | Type | Purpose |
|--------|------|---------|
| split_id | TEXT PK, FK → splits | One row per split |
| estimator | TEXT NOT NULL | Which estimator produced this ("kalman") |
| state_json | TEXT NOT NULL | JSON blob of estimator-specific state |
| marginal_return | REAL | Cached m_i for quick sorting/display |
| updated_at | TEXT NOT NULL | Last update timestamp |

JSON blob approach: no schema migration when estimator state shape changes. Estimators handle missing keys with defaults on deserialization. `marginal_return` is denormalized for efficient dashboard queries but always updated alongside `state_json` in the same transaction.

**Safety net:** `rebuild_state()` on the estimator can reconstruct `model_state` from the `attempts` table by replaying all observed times. If state gets corrupted, replay fixes it.

### New: `allocator_config` table

| Column | Type | Purpose |
|--------|------|---------|
| key | TEXT PK | Setting name |
| value | TEXT | Setting value |

Stores: active allocator name, active estimator name, session overrides from dashboard.

**Precedence:** On startup, read defaults from config.yaml. If `allocator_config` has overrides, use those instead. Overrides persist across sessions until explicitly changed via dashboard or config.yaml edit.

### Unchanged

- `splits` — no changes
- `attempts` — `rating` column becomes nullable (no longer populated), all other columns unchanged
- `sessions` — no changes
- `transitions` — no changes

---

## Scheduler Coordinator

**File:** `python/spinlab/scheduler.py` (rewritten)

Thin layer that holds one estimator instance + one allocator instance. Loads/saves model state from DB. Exposes to the orchestrator:

- `pick_next() → SplitWithModel | None` — load all active splits + model state, call `allocator.pick_next()` to get a `split_id`, then return the corresponding `SplitWithModel` from the loaded state list. Returns None if no active splits exist. The orchestrator converts this to a `SplitCommand` for TCP transmission (dropping estimator internals, keeping split_id, goal, reference_time_ms, state_path, auto_advance_delay_ms).
- `process_attempt(split_id, time_ms, completed) → None` — converts time_ms (int, milliseconds) to seconds (float) for the estimator. If `completed=false`, passes `observed_time=None` (estimator skips predict/update). Saves updated state to DB.
- `peek_next_n(n) → list[SplitWithModel]` — for dashboard queue preview
- `get_all_model_states() → list[SplitWithModel]` — for dashboard Model tab
- `switch_allocator(name)` / `switch_estimator(name)` — mid-session switching
- `rebuild_all_states()` — replay all attempts through estimator to reconstruct model_state table

---

## Orchestrator Changes

**Current flow:** pick → load → play → rate (R+D-pad) → update SM-2 → repeat

**New flow:** pick → load → play → completion detected → auto-advance (configurable delay) → update Kalman → repeat

### Specific changes

- **Drop rating wait.** After receiving `attempt_result` from Lua (which no longer includes a rating), immediately call `process_attempt(split_id, time_ms, completed)`.
- **Auto-advance delay.** After processing, wait `auto_advance_delay_s` (from config.yaml, default 2.0) before picking and loading the next split.
- **State file.** `orchestrator_state.json` updated to include estimator/allocator info:

```json
{
  "session_id": "...",
  "started_at": "...",
  "current_split_id": "...",
  "queue": ["...", "..."],
  "allocator": "greedy",
  "estimator": "kalman",
  "updated_at": "..."
}
```

- **Death/abort handling.** Still recorded as an attempt with `completed=false`. The coordinator passes `observed_time=None` to the estimator, which skips the Kalman predict/update but increments n_runs.
- **SplitCommand update.** `SplitCommand` in `models.py` gains an `auto_advance_delay_ms: int` field, populated from `config.yaml`'s `auto_advance_delay_s * 1000`, and included in `to_dict()` for the TCP `load_split` command.

---

## TCP Protocol Changes

### `load_split` command (orchestrator → Lua)

Added field:
- `auto_advance_delay_ms` (int) — how long to show result before transitioning

### `attempt_result` message (Lua → orchestrator)

Removed field:
- `rating` — no longer sent

Remaining fields unchanged: `type`, `split_id`, `time_ms`, `completed`.

---

## Lua Overlay & Auto-Advance

### Practice State Machine

**Old:** IDLE → LOADING → PLAYING → RATING → (loop)
**New:** IDLE → LOADING → PLAYING → RESULT → (loop)

### RESULT State

- Displays "Clear! 12.4s / 15.0s" (or "Abort 8.1s / 15.0s")
- Holds for `auto_advance_delay_ms` (received in load command)
- After delay: sends `attempt_result` to orchestrator, transitions to IDLE
- No input required — pure display state

### Overlay During PLAYING

- Line 1: Goal label (e.g., "Exit: Normal")
- Line 2: Elapsed vs reference timer (green if ahead, red if behind)
- No difficulty color tier (was broken, removed)

### Removed

- All R+D-pad rating input logic and debouncing code
- Rating prompt display ("R+< again R+v hard R+> good R+^ easy")
- RATING state entirely

### Future-Proofing

Overlay rendering is a function that draws N lines. Adding a drift indicator or why-picked line later is one additional `emu.drawString()` call — no structural change needed.

---

## Dashboard Changes

### Tab Structure

**Live | Model | Manage** — designed to accommodate a 4th tab if needed.

Tab bar at top, horizontally scrollable if more tabs are added. Active tab highlighted.

### Live Tab (reworked)

- **Header:** "SpinLab" + session timer + time saved this session
- **Allocator dropdown** in header — Greedy / Random / Round Robin. Changes take effect on next pick.
- **Current split:** goal label, attempt count this session
- **Insight card:** drift arrow (↓/→/↑ + rate), confidence label (uncertain/moderate/confident from P_dd), why-picked reason (from allocator)
- **Up next:** 2-3 splits from `peek_next_n`
- **Recent results:** last ~8 attempts, time vs reference (green/red). No rating badges.
- **Session stats footer:** "12/15 cleared | 23min"
- **When idle:** "No active session" with last session summary
- **When in reference mode:** "Reference Run" + sections captured count

### Model Tab (new)

- **Table** of all active splits ranked by marginal return
- **Per row:** split name/goal, μ (expected time), drift (d with arrow), confidence (from P_dd), marginal return (m_i), n_runs, gold time
- **Color coding:** green = improving (d < 0, confident), gray = flat/uncertain, red = regressing
- **Estimator dropdown** (Kalman only for now, UI supports future estimators)
- **Expandable rows:** click to see detail — raw drift value, P_dd, R, last 5 attempt times

### Manage Tab

Not in this feature branch. Stays as specced in the dashboard design doc for a future iteration.

### API Changes

| Endpoint | Method | Change |
|----------|--------|--------|
| `/api/state` | GET | Returns estimator + allocator info instead of SM-2 fields |
| `/api/model` | GET | **New.** All splits with full estimator state for Model tab |
| `/api/allocator` | POST | **New.** Switch allocator mid-session |
| `/api/estimator` | POST | **New.** Switch estimator mid-session |
| `/api/splits` | GET | Updated to include model state instead of schedule |

---

## Config Changes

**New fields in `config.yaml`:**

```yaml
estimator: kalman
allocator: greedy
auto_advance_delay_s: 2.0
```

These are defaults. Dashboard dropdowns can override mid-session (persisted to `allocator_config` table).

---

## File Change Summary

| Action | File | Role |
|--------|------|------|
| Create | `python/spinlab/estimators/__init__.py` | Estimator ABC + registry |
| Create | `python/spinlab/estimators/kalman.py` | KalmanEstimator + KalmanState |
| Create | `python/spinlab/allocators/__init__.py` | Allocator ABC + registry |
| Create | `python/spinlab/allocators/greedy.py` | Greedy allocator |
| Create | `python/spinlab/allocators/random.py` | Random allocator |
| Create | `python/spinlab/allocators/round_robin.py` | Round robin allocator |
| Rewrite | `python/spinlab/scheduler.py` | Thin coordinator (estimator + allocator) |
| Modify | `python/spinlab/db.py` | Drop schedule, add model_state + allocator_config |
| Modify | `python/spinlab/orchestrator.py` | Drop ratings, auto-advance, new coordinator |
| Modify | `python/spinlab/models.py` | Drop Schedule/Rating, add new types |
| Modify | `python/spinlab/dashboard.py` | New endpoints, reworked /api/state |
| Modify | `python/spinlab/static/app.js` | Model tab, reworked Live, allocator dropdown |
| Modify | `python/spinlab/static/index.html` | 3-tab layout |
| Modify | `python/spinlab/static/style.css` | Model tab styles, remove rating colors |
| Modify | `lua/spinlab.lua` | Drop RATING, add RESULT, remove R+D-pad input |
| Modify | `config.yaml` | New estimator/allocator/delay fields |
| Create | `tests/test_kalman.py` | Kalman estimator unit tests |
| Create | `tests/test_allocators.py` | Allocator unit tests |
| Modify | existing tests | Update for new interfaces |

---

## Build Order (Bottom-Up, Approach A)

1. **Estimator module + tests** — KalmanEstimator in isolation, pure math, full TDD
2. **Allocator module + tests** — Greedy, Random, Round Robin, tested against mock estimator output
3. **DB schema migration** — Drop schedule, create model_state + allocator_config, fresh start
4. **Scheduler coordinator rewrite** — Wire estimator + allocator, load/save from DB
5. **Orchestrator changes** — Drop ratings, auto-advance, new coordinator integration
6. **Smoke test gate** — Manual test: load emulator, verify the full loop works end-to-end
7. **Lua overlay + auto-advance** — Drop RATING state, add RESULT, remove input code
8. **Dashboard rework** — Model tab, reworked Live tab, allocator/estimator dropdowns, new API endpoints

Each step is independently testable before the next. Step 6 is a manual checkpoint before investing in UI polish.

---

## Feature Branch

All work on branch `kalman` off `main`.

---

## Supersedes

This spec supersedes the tab structure in `docs/superpowers/specs/2026-03-12-dashboard-design.md` (was 2 tabs: Live | Manage, now 3 tabs: Live | Model | Manage). All other aspects of the dashboard design spec remain valid.
