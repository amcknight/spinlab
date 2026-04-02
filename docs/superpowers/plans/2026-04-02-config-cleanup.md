# Config Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract magic numbers into named constants, add a tunable-params system to estimators, and expose Kalman filter knobs in the dashboard with live rebuild.

**Architecture:** Estimators declare their tunable params via a `declared_params()` method returning `ParamDef` objects. Params are stored in the `allocator_config` DB table (same pattern as allocator weights). Dashboard Model tab gets a collapsible tuning panel with sliders. Param changes trigger `rebuild_all_states()` for instant feedback.

**Tech Stack:** Python 3.11+ / FastAPI / SQLite / Vanilla ES6 JS / Lua

**Spec:** `docs/superpowers/specs/2026-04-02-config-cleanup-design.md`

---

## File Map

**Create:**
- `tests/test_estimator_params.py` — tests for ParamDef, declared_params, params-aware rebuild

**Modify:**
- `python/spinlab/estimators/__init__.py` — add ParamDef dataclass, `declared_params()` to ABC, `params` arg on rebuild/init/process
- `python/spinlab/estimators/kalman.py` — declared_params, params-aware rebuild/init/process, delete R_REESTIMATE_INTERVAL, change DEFAULT_D to 0.0, remove confidence labels from drift_info
- `python/spinlab/estimators/exp_decay.py` — add `params=None` to rebuild_state/init_state/process_attempt signatures
- `python/spinlab/estimators/rolling_mean.py` — add `params=None` to rebuild_state/init_state/process_attempt signatures
- `python/spinlab/scheduler.py` — load params from DB, pass through to estimators
- `python/spinlab/dashboard.py` — add GET/POST `/api/estimator-params` endpoints
- `python/spinlab/static/index.html` — add tuning panel HTML
- `python/spinlab/static/model.js` — tuning panel JS, remove drift threshold logic
- `python/spinlab/static/style.css` — tuning panel styles, remove drift CSS classes
- `python/spinlab/static/api.js` — name TOAST_TIMEOUT_MS and FALLBACK_POLL_MS constants
- `python/spinlab/session_manager.py` — name RECENT_ATTEMPTS_LIMIT constant
- `python/spinlab/practice.py` — name SEGMENT_LOAD_TIMEOUT_S constant
- `python/spinlab/sse.py` — name SSE_QUEUE_MAX constant
- `python/spinlab/db/attempts.py` — name RECENT_ATTEMPTS_DB_LIMIT constant
- `python/spinlab/db/sessions.py` — name SESSION_HISTORY_LIMIT constant
- `python/spinlab/cli.py` — name SOCKET_CONNECT_TIMEOUT_S constant
- `python/spinlab/dashboard.py` — name TCP_CONNECT_TIMEOUT_S, TCP_RETRY_DELAY_S, TCP_EVENT_TIMEOUT_S, SSE_KEEPALIVE_S constants
- `lua/spinlab.lua` — name AUTO_ADVANCE_DEFAULT_MS, REPLAY_PROGRESS_INTERVAL_MS constants
- `tests/test_kalman.py` — update tests for new DEFAULT_D=0.0 and params-aware signatures
- `tests/test_estimator_sanity.py` — update helper to pass params=None through signatures

---

### Task 1: ParamDef and ABC Changes

**Files:**
- Modify: `python/spinlab/estimators/__init__.py:14-79`
- Create: `tests/test_estimator_params.py`

- [ ] **Step 1: Write the failing test for ParamDef**

```python
# tests/test_estimator_params.py
"""Tests for estimator tunable params system."""
import pytest
from spinlab.estimators import ParamDef, Estimator, get_estimator, list_estimators

# Force registration
from spinlab.estimators.kalman import KalmanEstimator  # noqa: F401
from spinlab.estimators.rolling_mean import RollingMeanEstimator  # noqa: F401
try:
    from spinlab.estimators.exp_decay import ExpDecayEstimator  # noqa: F401
except ImportError:
    pass


class TestParamDef:
    def test_create_param_def(self):
        p = ParamDef(
            name="R", display_name="Obs. Noise", default=25.0,
            min_val=0.01, max_val=1000.0, step=0.1,
            description="How noisy individual attempts are.",
        )
        assert p.name == "R"
        assert p.default == 25.0
        assert p.min_val == 0.01

    def test_param_def_to_dict(self):
        p = ParamDef(
            name="R", display_name="Obs. Noise", default=25.0,
            min_val=0.01, max_val=1000.0, step=0.1,
            description="How noisy individual attempts are.",
        )
        d = p.to_dict()
        assert d["name"] == "R"
        assert d["default"] == 25.0
        assert d["min"] == 0.01
        assert d["max"] == 1000.0


class TestDeclaredParamsABC:
    def test_all_estimators_return_list(self):
        for name in list_estimators():
            est = get_estimator(name)
            params = est.declared_params()
            assert isinstance(params, list)
            for p in params:
                assert isinstance(p, ParamDef)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_estimator_params.py -v`
Expected: FAIL with `ImportError: cannot import name 'ParamDef'`

- [ ] **Step 3: Implement ParamDef and ABC changes**

In `python/spinlab/estimators/__init__.py`, add `ParamDef` dataclass before `EstimatorState`:

```python
@dataclass
class ParamDef:
    """Describes a tunable estimator parameter."""
    name: str
    display_name: str
    default: float
    min_val: float
    max_val: float
    step: float
    description: str

    def to_dict(self) -> dict:
        return {
            "name": self.name, "display_name": self.display_name,
            "default": self.default, "min": self.min_val, "max": self.max_val,
            "step": self.step, "description": self.description,
        }
```

Add `declared_params` to `Estimator` class (non-abstract, default returns `[]`):

```python
def declared_params(self) -> list["ParamDef"]:
    """Tunable params with metadata. Default: no params."""
    return []
```

Add `params: dict | None = None` to the signatures of `init_state`, `process_attempt`, and `rebuild_state` in the ABC:

```python
@abstractmethod
def init_state(
    self, first_attempt: "AttemptRecord", priors: dict,
    params: dict | None = None,
) -> EstimatorState:
    ...

@abstractmethod
def process_attempt(
    self,
    state: EstimatorState,
    new_attempt: "AttemptRecord",
    all_attempts: list["AttemptRecord"],
    params: dict | None = None,
) -> EstimatorState:
    ...

@abstractmethod
def rebuild_state(
    self, attempts: list["AttemptRecord"],
    params: dict | None = None,
) -> EstimatorState:
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_estimator_params.py -v`
Expected: PASS (ParamDef created, declared_params returns [] for all estimators)
Note: This will fail because the concrete estimators don't have `params` in their signatures yet. That's expected — we fix them in Task 2 and 3.

- [ ] **Step 5: Add params=None to RollingMean and ExpDecay signatures**

In `python/spinlab/estimators/rolling_mean.py`, add `params: dict | None = None` to `init_state` (line 35), `process_attempt` (line 38-40), and `rebuild_state` (line 94). The implementations don't use it — just accept and ignore:

```python
def init_state(self, first_attempt: AttemptRecord, priors: dict,
               params: dict | None = None) -> RollingMeanState:
    return RollingMeanState(n_completed=1, n_attempts=1)

def process_attempt(
    self, state: RollingMeanState, new_attempt: AttemptRecord,
    all_attempts: list[AttemptRecord],
    params: dict | None = None,
) -> RollingMeanState:
    n_completed = state.n_completed + (1 if new_attempt.completed else 0)
    return RollingMeanState(n_completed=n_completed, n_attempts=state.n_attempts + 1)

def rebuild_state(self, attempts: list[AttemptRecord],
                  params: dict | None = None) -> RollingMeanState:
    n_completed = sum(1 for a in attempts if a.completed)
    return RollingMeanState(n_completed=n_completed, n_attempts=len(attempts))
```

In `python/spinlab/estimators/exp_decay.py`, same pattern for `init_state` (line 113), `process_attempt` (line 116-118), and `rebuild_state` (line 161):

```python
def init_state(self, first_attempt: AttemptRecord, priors: dict,
               params: dict | None = None) -> ExpDecayState:
    return ExpDecayState(n_completed=1, n_attempts=1)

def process_attempt(
    self, state: ExpDecayState, new_attempt: AttemptRecord,
    all_attempts: list[AttemptRecord],
    params: dict | None = None,
) -> ExpDecayState:
    # ... existing body unchanged ...

def rebuild_state(self, attempts: list[AttemptRecord],
                  params: dict | None = None) -> ExpDecayState:
    # ... existing body unchanged ...
```

- [ ] **Step 6: Run all tests to verify nothing broke**

Run: `pytest tests/test_estimator_params.py tests/test_estimator_sanity.py tests/test_rolling_mean.py tests/test_exp_decay.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add python/spinlab/estimators/__init__.py python/spinlab/estimators/rolling_mean.py python/spinlab/estimators/exp_decay.py tests/test_estimator_params.py
git commit -m "feat: add ParamDef and params arg to estimator ABC"
```

---

### Task 2: Kalman Params-Aware Implementation

**Files:**
- Modify: `python/spinlab/estimators/kalman.py:14-226`
- Modify: `tests/test_kalman.py`
- Modify: `tests/test_estimator_params.py`

- [ ] **Step 1: Write failing tests for Kalman declared_params and params-aware rebuild**

Append to `tests/test_estimator_params.py`:

```python
from spinlab.estimators.kalman import KalmanEstimator, KalmanState
from spinlab.models import AttemptRecord


def _attempt(time_ms: int | None, completed: bool) -> AttemptRecord:
    clean = time_ms if completed and time_ms is not None else None
    return AttemptRecord(
        time_ms=time_ms, completed=completed, deaths=0,
        clean_tail_ms=clean, created_at="2026-01-01T00:00:00",
    )


class TestKalmanDeclaredParams:
    def test_returns_params(self):
        est = KalmanEstimator()
        params = est.declared_params()
        assert len(params) == 7
        names = {p.name for p in params}
        assert names == {"D0", "R", "P_D0", "Q_mm", "Q_dd", "R_floor", "R_blend"}

    def test_defaults_match_module_constants(self):
        est = KalmanEstimator()
        params = est.declared_params()
        by_name = {p.name: p for p in params}
        assert by_name["D0"].default == 0.0
        assert by_name["R"].default == 25.0
        assert by_name["Q_mm"].default == 0.1


class TestKalmanParamsAwareRebuild:
    def test_rebuild_with_custom_D0(self):
        est = KalmanEstimator()
        attempts = [_attempt(12000, True), _attempt(11000, True), _attempt(10000, True)]
        state_default = est.rebuild_state(attempts)
        state_custom = est.rebuild_state(attempts, params={"D0": -2.0})
        # Different D0 should produce different mu after processing
        assert state_default.mu != state_custom.mu

    def test_rebuild_with_custom_R(self):
        est = KalmanEstimator()
        attempts = [_attempt(12000, True), _attempt(11000, True), _attempt(10000, True)]
        state_low_R = est.rebuild_state(attempts, params={"R": 1.0})
        state_high_R = est.rebuild_state(attempts, params={"R": 100.0})
        # Lower R trusts data more, should track observations more closely
        assert state_low_R.mu != state_high_R.mu

    def test_rebuild_with_no_params_uses_defaults(self):
        est = KalmanEstimator()
        attempts = [_attempt(12000, True), _attempt(11000, True)]
        state_none = est.rebuild_state(attempts, params=None)
        state_empty = est.rebuild_state(attempts, params={})
        assert state_none.mu == state_empty.mu


class TestKalmanRReestimateEveryAttempt:
    def test_r_changes_after_second_attempt(self):
        """R should re-estimate on every completed attempt, not just every 10th."""
        est = KalmanEstimator()
        a1 = _attempt(12000, True)
        state = est.init_state(a1, priors={})
        initial_R = state.R
        # Feed a few attempts — R should change before attempt 10
        for i in range(5):
            a = _attempt(12000 - (i + 1) * 500, True)
            state = est.process_attempt(state, a, [a1])
        assert state.R != initial_R, "R should re-estimate before 10 attempts"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_estimator_params.py::TestKalmanDeclaredParams -v`
Expected: FAIL (declared_params returns [])

- [ ] **Step 3: Implement Kalman changes**

In `python/spinlab/estimators/kalman.py`:

**Change defaults section (lines 13-21):**
```python
# === Defaults ===
DEFAULT_D = 0.0
DEFAULT_R = 25.0
DEFAULT_P_D0 = 1.0
DEFAULT_Q_MM = 0.1
DEFAULT_Q_MD = 0.0
DEFAULT_Q_DD = 0.01
R_FLOOR = 1.0
R_BLEND = 0.3
CI_MULTIPLIER = 1.96
```

Delete `R_REESTIMATE_INTERVAL = 10`.

**Add declared_params method to KalmanEstimator class (after `display_name`):**
```python
def declared_params(self) -> list["ParamDef"]:
    from spinlab.estimators import ParamDef
    return [
        ParamDef("D0", "Initial Drift", 0.0, -5.0, 5.0, 0.1,
                 "Assumed improvement rate before data (seconds/attempt). 0 = no assumption."),
        ParamDef("R", "Obs. Noise", 25.0, 0.01, 1000.0, 0.1,
                 "How noisy individual attempts are. Higher = smoother, slower to react."),
        ParamDef("P_D0", "Drift Variance", 1.0, 0.01, 50.0, 0.1,
                 "Initial uncertainty about drift. Higher = more willing to learn drift from data."),
        ParamDef("Q_mm", "Process Noise (Mean)", 0.1, 0.001, 10.0, 0.01,
                 "How fast true skill is expected to change. Higher = more reactive."),
        ParamDef("Q_dd", "Process Noise (Drift)", 0.01, 0.001, 5.0, 0.001,
                 "How fast drift itself changes. Higher = trend estimates shift faster."),
        ParamDef("R_floor", "Noise Floor", 1.0, 0.01, 10.0, 0.01,
                 "Minimum observation noise. Prevents filter from over-trusting single attempts."),
        ParamDef("R_blend", "R Learning Rate", 0.3, 0.01, 1.0, 0.01,
                 "How fast observation noise adapts. 1.0 = fully trust new estimate."),
    ]
```

**Add helper to resolve params:**
```python
def _resolve_params(self, params: dict | None) -> dict:
    """Merge user params with defaults from declared_params."""
    defaults = {p.name: p.default for p in self.declared_params()}
    if params:
        defaults.update(params)
    return defaults
```

**Update `_reestimate_R` (lines 107-112) — use params for R_floor and R_blend:**
```python
def _reestimate_R(self, state: KalmanState, predicted: KalmanState,
                  observed_time: float, r_floor: float, r_blend: float) -> KalmanState:
    innovation_sq = (observed_time - predicted.mu) ** 2
    R_est = innovation_sq - predicted.P_mm
    R_new = max(R_est, r_floor)
    R_blended = (1 - r_blend) * state.R + r_blend * R_new
    return replace(state, R=max(R_blended, r_floor))
```

**Update `init_state` (lines 114-126) — accept and use params:**
```python
def init_state(self, first_attempt: AttemptRecord, priors: dict,
               params: dict | None = None) -> KalmanState:
    p = self._resolve_params(params)
    first_time = first_attempt.time_ms / 1000.0
    d = priors.get("d", p["D0"])
    R = priors.get("R", p["R"])
    Q_mm = priors.get("Q_mm", p["Q_mm"])
    Q_md = priors.get("Q_md", DEFAULT_Q_MD)
    Q_dd = priors.get("Q_dd", p["Q_dd"])
    return KalmanState(
        mu=first_time, d=d,
        P_mm=R, P_md=0.0, P_dm=0.0, P_dd=p["P_D0"],
        R=R, Q_mm=Q_mm, Q_md=Q_md, Q_dm=Q_md, Q_dd=Q_dd,
        gold=first_time, n_completed=1, n_attempts=1,
    )
```

**Update `process_attempt` (lines 128-151) — accept params, re-estimate R every attempt:**
```python
def process_attempt(
    self, state: KalmanState, new_attempt: AttemptRecord,
    all_attempts: list[AttemptRecord],
    params: dict | None = None,
) -> KalmanState:
    observed_time = (
        new_attempt.time_ms / 1000.0
        if new_attempt.completed and new_attempt.time_ms is not None
        else None
    )
    if observed_time is None:
        return replace(state, n_attempts=state.n_attempts + 1)

    p = self._resolve_params(params)
    predicted = self._predict(state)
    updated = self._update(predicted, observed_time)
    n_completed = state.n_completed + 1
    gold = min(state.gold, observed_time)

    result = replace(updated,
        Q_mm=state.Q_mm, Q_md=state.Q_md, Q_dm=state.Q_dm, Q_dd=state.Q_dd,
        gold=gold, n_completed=n_completed, n_attempts=state.n_attempts + 1,
    )
    # Re-estimate R on every completed attempt
    if n_completed >= 2:
        result = self._reestimate_R(result, predicted, observed_time,
                                     r_floor=p["R_floor"], r_blend=p["R_blend"])
    return result
```

**Update `rebuild_state` (lines 214-225) — accept and pass params:**
```python
def rebuild_state(self, attempts: list[AttemptRecord],
                  params: dict | None = None) -> KalmanState:
    completed = [a for a in attempts if a.completed and a.time_ms is not None]
    if not completed:
        return KalmanState(n_attempts=len(attempts))
    first = completed[0]
    state = self.init_state(first, priors={}, params=params)
    first_idx = attempts.index(first)
    for a in attempts[:first_idx]:
        state = self.process_attempt(state, a, attempts, params=params)
    for a in attempts[first_idx + 1:]:
        state = self.process_attempt(state, a, attempts, params=params)
    return state
```

**Update `drift_info` (lines 166-186) — use CI_MULTIPLIER, remove confidence labels:**
```python
def drift_info(self, state: KalmanState) -> dict:
    import math
    p_dd_sqrt = math.sqrt(max(state.P_dd, 0.0))
    ci_lower = state.d - CI_MULTIPLIER * p_dd_sqrt
    ci_upper = state.d + CI_MULTIPLIER * p_dd_sqrt
    if state.d < 0:
        label = "improving"
    elif state.d > 0:
        label = "regressing"
    else:
        label = "flat"
    return {
        "drift": state.d, "ci_lower": ci_lower, "ci_upper": ci_upper,
        "label": label,
    }
```

**Update `get_population_priors` (line 189-198) — use DEFAULT_D instead of hardcoded -0.5, remove R_REESTIMATE_INTERVAL usage:**

The maturity threshold (`R_REESTIMATE_INTERVAL` was used on line 189) needs a named constant. Replace with a simple named constant:

```python
MATURITY_THRESHOLD = 10

def get_population_priors(self, all_states: list[KalmanState]) -> dict:
    mature = [s for s in all_states if s.n_completed >= MATURITY_THRESHOLD]
    if not mature:
        return {"d": DEFAULT_D, "R": DEFAULT_R, "Q_mm": DEFAULT_Q_MM, "Q_dd": DEFAULT_Q_DD}
    n = len(mature)
    return {
        "d": sum(s.d for s in mature) / n,
        "R": sum(s.R for s in mature) / n,
        "Q_mm": sum(s.Q_mm for s in mature) / n,
        "Q_dd": sum(s.Q_dd for s in mature) / n,
    }
```

- [ ] **Step 4: Update existing Kalman tests**

In `tests/test_kalman.py`:

Update `test_produces_model_output` (line 52-54) — DEFAULT_D is now 0.0 not -0.5:
```python
def test_produces_model_output(self):
    est = KalmanEstimator()
    a1 = _attempt(12000, True)
    state = est.init_state(a1, priors={})
    out = est.model_output(state, [a1])
    assert isinstance(out, ModelOutput)
    # expected = (mu + d) * 1000 = (12.0 + 0.0) * 1000 = 12000
    assert out.total.expected_ms == pytest.approx(12000.0)
    assert out.total.ms_per_attempt == pytest.approx(0.0)  # -d * 1000 = 0
    assert out.total.floor_ms is None
```

Update `test_expected_predicts_forward` (line 82):
```python
def test_expected_predicts_forward(self):
    """expected_ms should be mu + d (predicted next), not just mu (current)."""
    est = KalmanEstimator()
    a1 = _attempt(12000, True)
    state = est.init_state(a1, priors={})
    # mu=12.0, d=0.0 after init, so predicted next = 12.0s = 12000ms
    out = est.model_output(state, [a1])
    assert out.total.expected_ms == pytest.approx((state.mu + state.d) * 1000)
```

Update `test_no_mature_states_returns_defaults` (line 120):
```python
def test_no_mature_states_returns_defaults(self, tmp_path):
    from spinlab.db import Database
    db = Database(str(tmp_path / "test.db"))
    db.upsert_game("g1", "Game", "any%")
    est = KalmanEstimator()
    priors = est.get_priors(db, "g1")
    assert priors["d"] == 0.0
    assert priors["R"] == 25.0
```

Update the constant-data tolerance test in `tests/test_estimator_sanity.py` (lines 224-228) — with D0=0.0 instead of -0.5, the Kalman filter should converge closer to 0 for constant data. The existing 200ms tolerance should still be fine, but the comment changes:
```python
# Kalman starts with d=0.0, should stay near 0 for constant data.
assert abs(out.total.ms_per_attempt) < 200, (
    f"{estimator.name}: ms_per_attempt = {out.total.ms_per_attempt} for constant data"
)
```

- [ ] **Step 5: Run all estimator tests**

Run: `pytest tests/test_estimator_params.py tests/test_kalman.py tests/test_estimator_sanity.py tests/test_exp_decay.py tests/test_rolling_mean.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/estimators/kalman.py tests/test_estimator_params.py tests/test_kalman.py tests/test_estimator_sanity.py
git commit -m "feat: Kalman declared_params, params-aware rebuild, D0=0.0, R reestimate every attempt"
```

---

### Task 3: Scheduler Params Wiring

**Files:**
- Modify: `python/spinlab/scheduler.py:100-179`
- Modify: `tests/test_scheduler_kalman.py`

- [ ] **Step 1: Write failing test for params-aware rebuild**

Check existing test structure first. Append to `tests/test_estimator_params.py`:

```python
class TestSchedulerParamsWiring:
    def test_rebuild_all_states_passes_params(self, tmp_path):
        """Scheduler.rebuild_all_states should load and pass estimator params."""
        import json
        from spinlab.db import Database
        from spinlab.models import Segment
        from spinlab.scheduler import Scheduler

        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "Game", "any%")
        seg = Segment(
            id="s1", game_id="g1", level_number=1,
            start_type="entrance", start_ordinal=0,
            end_type="checkpoint", end_ordinal=0,
        )
        db.upsert_segment(seg)
        db.insert_attempt("s1", 12000, True, 0, 12000, "practice")
        db.insert_attempt("s1", 11000, True, 0, 11000, "practice")
        db.insert_attempt("s1", 10000, True, 0, 10000, "practice")

        sched = Scheduler(db, "g1")

        # Rebuild with default params
        sched.rebuild_all_states()
        row_default = db.load_model_state("s1", "kalman")
        state_default = json.loads(row_default["state_json"])

        # Save custom params and rebuild
        db.save_allocator_config("estimator_params:kalman", json.dumps({"D0": -2.0}))
        sched.rebuild_all_states()
        row_custom = db.load_model_state("s1", "kalman")
        state_custom = json.loads(row_custom["state_json"])

        # Different D0 should produce different state
        assert state_default["mu"] != state_custom["mu"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_estimator_params.py::TestSchedulerParamsWiring -v`
Expected: FAIL (scheduler doesn't load or pass params yet)

- [ ] **Step 3: Implement scheduler params wiring**

In `python/spinlab/scheduler.py`:

Add a method to load params for a given estimator:

```python
def _load_estimator_params(self, estimator_name: str) -> dict | None:
    """Load tunable params from DB for an estimator, or None for defaults."""
    raw = self.db.load_allocator_config(f"estimator_params:{estimator_name}")
    if raw:
        return json.loads(raw)
    return None
```

Update `process_attempt` (around line 120-131) to pass params:

```python
for est in [get_estimator(n) for n in list_estimators()]:
    row = self.db.load_model_state(segment_id, est.name)
    params = self._load_estimator_params(est.name)

    if row and row["state_json"]:
        state = EstimatorState.deserialize(est.name, row["state_json"])
        state = est.process_attempt(state, new_attempt, all_attempts, params=params)
    else:
        if completed and time_ms is not None:
            priors = est.get_priors(self.db, self.game_id)
            state = est.init_state(new_attempt, priors, params=params)
        else:
            state = est.rebuild_state([new_attempt], params=params)
            output = est.model_output(state, all_attempts_with_new)
            self.db.save_model_state(
                segment_id, est.name,
                json.dumps(state.to_dict()), json.dumps(output.to_dict()),
            )
            continue
```

Update `rebuild_all_states` (lines 165-179) to pass params:

```python
def rebuild_all_states(self) -> None:
    segments = self.db.get_all_segments_with_model(self.game_id)
    for row in segments:
        segment_id = row["id"]
        attempt_rows = self.db.get_segment_attempts(segment_id)
        if not attempt_rows:
            continue
        all_attempts = _attempts_from_rows(attempt_rows)
        for est in [get_estimator(n) for n in list_estimators()]:
            params = self._load_estimator_params(est.name)
            state = est.rebuild_state(all_attempts, params=params)
            output = est.model_output(state, all_attempts)
            self.db.save_model_state(
                segment_id, est.name,
                json.dumps(state.to_dict()), json.dumps(output.to_dict()),
            )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_estimator_params.py tests/test_scheduler_kalman.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/scheduler.py tests/test_estimator_params.py
git commit -m "feat: scheduler loads and passes estimator params through rebuild"
```

---

### Task 4: Dashboard API Endpoints

**Files:**
- Modify: `python/spinlab/dashboard.py:191-200`
- Modify: `tests/test_estimator_params.py`

- [ ] **Step 1: Write failing test for estimator-params endpoints**

Append to `tests/test_estimator_params.py`:

```python
class TestEstimatorParamsAPI:
    @pytest.fixture
    def client(self, tmp_path):
        from spinlab.db import Database
        from spinlab.dashboard import create_app
        from starlette.testclient import TestClient

        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "TestGame", "any%")
        app = create_app(db=db, default_category="any%")
        client = TestClient(app)
        # Set game so scheduler initializes
        client.post("/api/practice/start")  # This may fail but sets game context
        return client, db

    def test_get_estimator_params_returns_schema(self, tmp_path):
        from spinlab.db import Database
        from spinlab.dashboard import create_app
        from starlette.testclient import TestClient

        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "TestGame", "any%")
        app = create_app(db=db, default_category="any%")
        client = TestClient(app)
        # Manually set game_id on session
        app.state.session.game_id = "g1"
        app.state.session.game_name = "TestGame"

        resp = client.get("/api/estimator-params")
        assert resp.status_code == 200
        data = resp.json()
        assert "estimator" in data
        assert "params" in data
        assert isinstance(data["params"], list)

    def test_post_estimator_params_saves(self, tmp_path):
        from spinlab.db import Database
        from spinlab.dashboard import create_app
        from starlette.testclient import TestClient

        db = Database(str(tmp_path / "test.db"))
        db.upsert_game("g1", "TestGame", "any%")
        app = create_app(db=db, default_category="any%")
        client = TestClient(app)
        app.state.session.game_id = "g1"
        app.state.session.game_name = "TestGame"

        resp = client.post("/api/estimator-params", json={"params": {"D0": 1.0}})
        assert resp.status_code == 200

        # Verify it was saved
        raw = db.load_allocator_config("estimator_params:kalman")
        assert raw is not None
        import json
        saved = json.loads(raw)
        assert saved["D0"] == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_estimator_params.py::TestEstimatorParamsAPI -v`
Expected: FAIL (404, endpoint doesn't exist)

- [ ] **Step 3: Implement endpoints**

In `python/spinlab/dashboard.py`, after the `switch_estimator` endpoint (around line 200), add:

```python
@app.get("/api/estimator-params")
def get_estimator_params():
    sched = session._get_scheduler()
    est = sched.estimator
    declared = est.declared_params()
    raw = db.load_allocator_config(f"estimator_params:{est.name}")
    saved = json.loads(raw) if raw else {}
    return {
        "estimator": est.name,
        "params": [
            {
                **p.to_dict(),
                "value": saved.get(p.name, p.default),
            }
            for p in declared
        ],
    }

@app.post("/api/estimator-params")
def set_estimator_params(body: dict):
    sched = session._get_scheduler()
    est = sched.estimator
    params = body.get("params", {})
    # Validate param names
    valid_names = {p.name for p in est.declared_params()}
    for name in params:
        if name not in valid_names:
            raise HTTPException(status_code=400, detail=f"Unknown param: {name}")
    db.save_allocator_config(f"estimator_params:{est.name}", json.dumps(params))
    sched.rebuild_all_states()
    return {"status": "ok"}
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_estimator_params.py::TestEstimatorParamsAPI -v`
Expected: PASS

- [ ] **Step 5: Run full test suite to check for regressions**

Run: `pytest tests/ -v --timeout=30`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/dashboard.py tests/test_estimator_params.py
git commit -m "feat: GET/POST /api/estimator-params endpoints"
```

---

### Task 5: Dashboard Tuning Panel UI

**Files:**
- Modify: `python/spinlab/static/index.html:66-70`
- Modify: `python/spinlab/static/model.js`
- Modify: `python/spinlab/static/style.css`

- [ ] **Step 1: Add tuning panel HTML**

In `python/spinlab/static/index.html`, after the `model-header` div (line 70, after `</select>`) and before the model table (line 71), add:

```html
      </div>
      <div id="tuning-panel" class="tuning-panel collapsed">
        <button id="tuning-toggle" class="tuning-toggle">
          <span class="tuning-caret">&#9656;</span> Tuning
        </button>
        <div id="tuning-body" class="tuning-body" style="display:none">
          <div id="tuning-params"></div>
          <div class="tuning-actions">
            <button id="btn-tuning-apply" class="btn-primary btn-sm">Apply</button>
            <button id="btn-tuning-reset" class="btn-sm">Reset Defaults</button>
          </div>
        </div>
      </div>
      <table id="model-table">
```

This goes between the closing `</div>` of `model-header` and the opening `<table id="model-table">`.

- [ ] **Step 2: Add tuning panel CSS**

Append to `python/spinlab/static/style.css`:

```css
/* Tuning panel */
.tuning-panel { margin: 8px 0; }
.tuning-toggle {
  background: none; border: none; color: var(--text-dim);
  cursor: pointer; font-size: 13px; padding: 4px 0;
}
.tuning-toggle:hover { color: var(--text); }
.tuning-caret { display: inline-block; transition: transform 0.2s; }
.tuning-panel:not(.collapsed) .tuning-caret { transform: rotate(90deg); }
.tuning-body { padding: 8px 0; }
.tuning-row {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 6px; font-size: 12px;
}
.tuning-label { min-width: 120px; color: var(--text-dim); }
.tuning-slider { flex: 1; accent-color: var(--accent); }
.tuning-value {
  width: 64px; background: var(--surface); border: 1px solid var(--card);
  color: var(--text); text-align: right; padding: 2px 4px; font-size: 12px;
}
.tuning-desc { font-size: 11px; color: var(--text-dim); margin: 0 0 8px 128px; }
.tuning-actions { display: flex; gap: 8px; margin-top: 8px; }
.tuning-empty { color: var(--text-dim); font-size: 12px; font-style: italic; }
```

- [ ] **Step 3: Add tuning panel JavaScript**

In `python/spinlab/static/model.js`, add the tuning panel logic. At the top, add a state variable:

```javascript
let _tuningParams = null;
```

Add functions before `initModelTab`:

```javascript
async function fetchTuningParams() {
  const data = await fetchJSON('/api/estimator-params');
  if (!data) return;
  _tuningParams = data;
  renderTuningParams(data);
}

function renderTuningParams(data) {
  const container = document.getElementById('tuning-params');
  if (!container) return;
  container.innerHTML = '';
  if (!data.params || data.params.length === 0) {
    container.innerHTML = '<p class="tuning-empty">No tunable parameters</p>';
    return;
  }
  data.params.forEach(p => {
    const row = document.createElement('div');
    row.className = 'tuning-row';
    row.innerHTML =
      '<span class="tuning-label">' + p.display_name + '</span>' +
      '<input type="range" class="tuning-slider" ' +
        'data-param="' + p.name + '" ' +
        'min="' + p.min + '" max="' + p.max + '" step="' + p.step + '" ' +
        'value="' + p.value + '">' +
      '<input type="number" class="tuning-value" ' +
        'data-param="' + p.name + '" ' +
        'min="' + p.min + '" max="' + p.max + '" step="' + p.step + '" ' +
        'value="' + p.value + '">';
    container.appendChild(row);

    // Sync slider and input
    const slider = row.querySelector('.tuning-slider');
    const input = row.querySelector('.tuning-value');
    slider.addEventListener('input', () => { input.value = slider.value; });
    input.addEventListener('input', () => { slider.value = input.value; });
  });
}

function collectTuningParams() {
  const params = {};
  document.querySelectorAll('#tuning-params .tuning-slider').forEach(slider => {
    params[slider.dataset.param] = parseFloat(slider.value);
  });
  return params;
}

async function applyTuningParams() {
  const params = collectTuningParams();
  await postJSON('/api/estimator-params', { params });
  fetchModel();
}

async function resetTuningDefaults() {
  if (!_tuningParams) return;
  _tuningParams.params.forEach(p => {
    const slider = document.querySelector('.tuning-slider[data-param="' + p.name + '"]');
    const input = document.querySelector('.tuning-value[data-param="' + p.name + '"]');
    if (slider) slider.value = p.default;
    if (input) input.value = p.default;
  });
  await applyTuningParams();
}
```

Update `initModelTab` to wire up the tuning panel:

```javascript
export function initModelTab() {
  document.getElementById('estimator-select').addEventListener('change', async (e) => {
    await postJSON('/api/estimator', { name: e.target.value });
    fetchModel();
    fetchTuningParams();
  });
  document.getElementById('btn-practice-start').addEventListener('click', () =>
    postJSON('/api/practice/start'));
  document.getElementById('btn-practice-stop').addEventListener('click', () =>
    postJSON('/api/practice/stop'));

  // Tuning panel toggle
  const toggle = document.getElementById('tuning-toggle');
  const panel = document.getElementById('tuning-panel');
  const body = document.getElementById('tuning-body');
  if (toggle) {
    toggle.addEventListener('click', () => {
      panel.classList.toggle('collapsed');
      body.style.display = panel.classList.contains('collapsed') ? 'none' : '';
    });
  }
  document.getElementById('btn-tuning-apply')?.addEventListener('click', applyTuningParams);
  document.getElementById('btn-tuning-reset')?.addEventListener('click', resetTuningDefaults);

  fetchTuningParams();
}
```

- [ ] **Step 4: Test manually in browser**

Run: `python -m spinlab dashboard` and open `http://127.0.0.1:15483`
Expected: Model tab shows collapsible "Tuning" section below estimator dropdown. Clicking it reveals sliders for Kalman params. Apply button triggers rebuild and model table updates.

- [ ] **Step 5: Commit**

```bash
git add python/spinlab/static/index.html python/spinlab/static/model.js python/spinlab/static/style.css
git commit -m "feat: dashboard tuning panel with per-estimator param sliders"
```

---

### Task 6: Remove Drift Threshold and Confidence Labels

**Files:**
- Modify: `python/spinlab/static/model.js:136-142, 184-189`
- Modify: `python/spinlab/static/style.css:261-263`
- Modify: `python/spinlab/estimators/kalman.py` (drift_info)

- [ ] **Step 1: Remove drift threshold from model table**

In `python/spinlab/static/model.js`, replace the drift classification block (lines 136-142) with simple display:

```javascript
  data.segments.forEach(s => {
    const tr = document.createElement('tr');
    const sel = s.model_outputs[s.selected_model];

    tr.innerHTML =
      '<td>' + segmentName(s) + '</td>' +
      '<td>' + formatTime(sel ? sel.expected_time_ms : null) + '</td>' +
      '<td>' + (sel && sel.ms_per_attempt != null ? sel.ms_per_attempt.toFixed(1) + ' ms/att' : '\u2014') + '</td>' +
      '<td>' + formatTime(sel ? sel.floor_estimate_ms : null) + '</td>' +
      '<td>' + s.n_completed + '</td>' +
      '<td>' + formatTime(s.gold_ms) + '</td>';
    body.appendChild(tr);
  });
```

This removes `improvClass`, `arrow`, `drift-row-*` class, and `drift-*` class from the trend cell.

- [ ] **Step 2: Remove drift threshold from practice card**

In `python/spinlab/static/model.js`, replace lines 184-189 with:

```javascript
  if (selOut) {
    const mpa = selOut.ms_per_attempt;
    insight.innerHTML = '<span>' + mpa.toFixed(1) + ' ms/att</span>';
  } else {
    insight.textContent = 'No data yet';
  }
```

- [ ] **Step 3: Remove drift CSS classes**

In `python/spinlab/static/style.css`, delete lines 261-263:

```css
.drift-improving { color: var(--green); }
.drift-regressing { color: var(--red); }
.drift-flat { color: var(--text-dim); }
```

Also search for and remove any `drift-row-*` styles if they exist.

- [ ] **Step 4: Verify drift_info in kalman.py already updated**

The confidence label removal was done in Task 2. Verify `drift_info` no longer returns `"confidence"` key.

- [ ] **Step 5: Run tests**

Run: `pytest tests/ -v --timeout=30`
Expected: All PASS (drift_info tests in test_kalman.py should still pass since they check for "drift", "label", "ci_lower" but not "confidence")

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/static/model.js python/spinlab/static/style.css
git commit -m "fix: remove arbitrary drift threshold labels and confidence qualifiers"
```

---

### Task 7: Name All Magic Numbers

**Files:**
- Modify: `python/spinlab/dashboard.py:48-53, 378`
- Modify: `python/spinlab/session_manager.py:131, 404`
- Modify: `python/spinlab/practice.py:103`
- Modify: `python/spinlab/sse.py:14`
- Modify: `python/spinlab/db/attempts.py:56`
- Modify: `python/spinlab/db/sessions.py:36`
- Modify: `python/spinlab/cli.py:112`
- Modify: `python/spinlab/static/api.js:13, 63`
- Modify: `lua/spinlab.lua:130, 141, 306, 1164`

- [ ] **Step 1: Python timing constants in dashboard.py**

At the top of `python/spinlab/dashboard.py` (after imports), add:

```python
TCP_CONNECT_TIMEOUT_S = 2
TCP_RETRY_DELAY_S = 2
TCP_EVENT_TIMEOUT_S = 1.0
SSE_KEEPALIVE_S = 30
```

Replace the magic numbers in `event_loop` (lines 48-53):
```python
async def event_loop(session: SessionManager, tcp: TcpManager) -> None:
    while True:
        if not tcp.is_connected:
            await tcp.connect(timeout=TCP_CONNECT_TIMEOUT_S)
            if not tcp.is_connected:
                await asyncio.sleep(TCP_RETRY_DELAY_S)
                continue
        try:
            event = await tcp.recv_event(timeout=TCP_EVENT_TIMEOUT_S)
```

Replace the SSE keepalive timeout (line 378):
```python
state = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_S)
```

- [ ] **Step 2: Python query/session constants**

In `python/spinlab/session_manager.py`, add at top and replace:
```python
RECENT_ATTEMPTS_LIMIT = 8
# ... then at line 131:
base["recent"] = self.db.get_recent_attempts(self.game_id, limit=RECENT_ATTEMPTS_LIMIT)
```

Add at top and replace timeout (line 404):
```python
PRACTICE_STOP_TIMEOUT_S = 5
# ... then:
await asyncio.wait_for(self.practice_task, timeout=PRACTICE_STOP_TIMEOUT_S)
```

In `python/spinlab/practice.py`, add at top and replace (line 103):
```python
SEGMENT_LOAD_TIMEOUT_S = 1.0
# ... then:
await asyncio.wait_for(self._result_event.wait(), timeout=SEGMENT_LOAD_TIMEOUT_S)
```

In `python/spinlab/sse.py`, add at top and replace (line 14):
```python
SSE_QUEUE_MAX = 16
# ... then:
def subscribe(self, maxsize: int = SSE_QUEUE_MAX) -> asyncio.Queue:
```

In `python/spinlab/db/attempts.py`, add at top and replace (line 56):
```python
RECENT_ATTEMPTS_DB_LIMIT = 8
# ... then:
def get_recent_attempts(self, game_id: str, limit: int = RECENT_ATTEMPTS_DB_LIMIT) -> list[dict]:
```

In `python/spinlab/db/sessions.py`, add at top and replace (line 36):
```python
SESSION_HISTORY_LIMIT = 10
# ... then:
def get_session_history(self, game_id: str, limit: int = SESSION_HISTORY_LIMIT) -> list[dict]:
```

In `python/spinlab/cli.py`, add at top and replace (line 112):
```python
SOCKET_CONNECT_TIMEOUT_S = 2
# ... then:
with socket.create_connection((tcp_host, tcp_port), timeout=SOCKET_CONNECT_TIMEOUT_S) as s:
```

- [ ] **Step 3: JavaScript constants**

In `python/spinlab/static/api.js`, add at top:
```javascript
const TOAST_TIMEOUT_MS = 8000;
const FALLBACK_POLL_MS = 5000;
```

Replace line 13: `setTimeout(() => el.classList.remove('visible'), TOAST_TIMEOUT_MS);`
Replace line 63: `}, FALLBACK_POLL_MS);`

- [ ] **Step 4: Lua constants**

In `lua/spinlab.lua`, at line 130 and 141 where `auto_advance_ms = 2000` appears, the value is already assigned to a field name. Add a constant at the config section top:

```lua
local AUTO_ADVANCE_DEFAULT_MS = 2000
```

Replace `auto_advance_ms = 2000` with `auto_advance_ms = AUTO_ADVANCE_DEFAULT_MS` at lines 130, 141, and the `or 2000` at line 306.

At line 1164 where `>= 100` appears, add near the replay section:
```lua
local REPLAY_PROGRESS_INTERVAL_MS = 100
```

And replace: `if now - replay.last_progress_ms >= REPLAY_PROGRESS_INTERVAL_MS then`

Note: `HEARTBEAT_INTERVAL` (line 845), `POKE_SETTLE_FRAMES` (poke_engine.lua line 44), `MAX_RECORDING_FRAMES` (line 26), and `CHAR_W` (line 315) are already named — skip these.

- [ ] **Step 5: Run all tests**

Run: `pytest tests/ -v --timeout=30`
Expected: All PASS (no behavioral changes, just constant extraction)

- [ ] **Step 6: Commit**

```bash
git add python/spinlab/dashboard.py python/spinlab/session_manager.py python/spinlab/practice.py python/spinlab/sse.py python/spinlab/db/attempts.py python/spinlab/db/sessions.py python/spinlab/cli.py python/spinlab/static/api.js lua/spinlab.lua
git commit -m "refactor: name all magic numbers as file-level constants"
```

---

### Task 8: Bump Cache Version and Final Verification

**Files:**
- Modify: `python/spinlab/static/index.html:7, 140`

- [ ] **Step 1: Bump cache buster**

In `python/spinlab/static/index.html`, change `?v=20` to `?v=21` on lines 7 and 140 (CSS and JS links).

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -v --timeout=30`
Expected: All PASS

- [ ] **Step 3: Verify no regressions in dashboard**

Run: `python -m spinlab dashboard` and manually verify:
- Model tab loads, tuning panel is collapsible
- Switching estimator reloads tuning params
- Kalman shows 7 sliders, others show "No tunable parameters"
- Apply button triggers rebuild, model table updates
- Reset Defaults restores values
- No drift arrows/colors in model table or practice card
- Trend column still shows numeric ms/att value

- [ ] **Step 4: Commit**

```bash
git add python/spinlab/static/index.html
git commit -m "chore: bump static cache version to v21"
```
