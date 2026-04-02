# Testability Refactors + Outside-View Model Sanity Tests

**Date:** 2026-04-02
**Scope:** Targeted refactors to improve testability, plus new test suites that serve as regression safety nets for upcoming intricate changes.

## Motivation

SpinLab is entering a phase of significant changes. Current tests are mostly "inside-view" (checking internal state mechanics) or rely on heavy mocks that can drift from real behavior. We need:
1. Refactors that make code easier to test in isolation
2. Tests that catch real breakage during refactoring — especially math/model correctness

---

## Part 1: Refactors

### 1A. Dedupe `handle_replay_error` / `handle_disconnect`

**File:** `capture_controller.py` lines 261-280

Both methods have identical bodies. Extract to `_finalize_or_delete_run(db)`.

### 1B. Fix double `get_all_segments_with_model` call

**File:** `session_manager.py` `_build_practice_state()` lines 135-172

Calls `db.get_all_segments_with_model(self.game_id)` at line 145 and again at line 170. Query once, reuse the result.

### 1C. Extract `validate_rom_path`

**File:** `dashboard.py` `launch_emulator()` lines 317-351

The path traversal check (lines 333-337) is security-relevant logic buried in an endpoint closure. Extract to a standalone function `validate_rom_path(rom_path: Path, rom_dir: Path) -> Path` that:
- Resolves both paths
- Raises `ValueError` if rom_path is outside rom_dir
- Returns the resolved rom_path on success

This makes the security check independently unit-testable.

### 1D. Move `/api/model` dict-building into `Scheduler.get_model_api_state()`

**File:** `dashboard.py` lines 145-176, `scheduler.py`

The `/api/model` endpoint builds a large dict inline by calling `sched.get_all_model_states()` and then manually serializing each `SegmentWithModel`. Move this into `Scheduler.get_model_api_state() -> dict` so:
- The serialization logic is unit-testable without HTTP
- Dashboard endpoint becomes a thin wrapper

---

## Part 2: Outside-View Estimator Sanity Tests

### Philosophy

These tests treat estimators as black boxes. They don't check internal state — they feed realistic attempt sequences and assert on properties that must hold for the *outputs* to make physical sense. All registered estimators are tested with the same scenarios via parametrize.

### Universal Invariants (must hold for ANY attempt sequence)

| ID | Invariant | Rationale |
|----|-----------|-----------|
| U1 | `expected_ms > 0` when non-None | Can't predict negative time |
| U2 | `floor_ms > 0` when non-None | Observed minimum is always positive |
| U3 | No `NaN` or `inf` in any Estimate field | Numeric sanity |
| U4 | `clean.floor_ms <= total.floor_ms` when both non-None | Clean tail is a subset of total time |

### Conditional Invariants (hold given specific input shapes)

| ID | Condition | Invariant | Rationale |
|----|-----------|-----------|-----------|
| C1 | All times identical (10 attempts at 10000ms) | `ms_per_attempt ≈ 0` (within tolerance) | No trend in flat data |
| C2 | Strictly decreasing times (15000 down to 6000) | `ms_per_attempt > 0` | Player is improving |
| C3 | Strictly increasing times (6000 up to 15000) | `ms_per_attempt < 0` | Player is regressing |
| C4 | Zero deaths, `clean_tail_ms == time_ms` | `clean ≈ total` estimates (or clean is None) | No death overhead to separate |
| C5 | All estimators given same monotonic data | Agree on sign of `ms_per_attempt` | Estimators may disagree on magnitude but not direction |

### Test Structure

- `tests/test_estimator_sanity.py`
- `@pytest.fixture(params=list_estimators())` to run every scenario against every estimator
- Helper to feed N attempts through `init_state` + `process_attempt` + `model_output`
- Tolerances: `abs(ms_per_attempt) < 50` for "approximately zero" in C1

### Scenarios

1. **Constant performer** — 10 completions at 10000ms, 0 deaths
2. **Steady improver** — 10 completions from 15000ms down to 6000ms, 0 deaths
3. **Steady regressor** — 10 completions from 6000ms up to 15000ms, 0 deaths
4. **Mixed with deaths** — 8 completions with varying deaths and clean_tail_ms
5. **Single attempt** — 1 completion at 12000ms (edge case for trend calculation)
6. **All incomplete** — 5 incomplete attempts (edge case: no completed data)
7. **Clean equals total** — 5 completions, 0 deaths, clean_tail_ms == time_ms

---

## Part 3: Additional Test Coverage

### 3A. SSE Broadcaster Tests (`tests/test_sse.py`)

Pure async unit tests for `SSEBroadcaster`:
- Subscribe/unsubscribe lifecycle
- Broadcast reaches all subscribers
- Full queue: old message dropped, new message delivered
- Dead subscriber cleanup on persistent full queue
- `has_subscribers` property accuracy

### 3B. `db/attempts.py` Coverage (`tests/test_db_attempts.py`)

Test the untested query methods with real SQLite:
- `get_segment_stats` — verify aggregation (count, avg, min) with mixed complete/incomplete
- `get_segment_stats` with `strat_version` filter
- `get_recent_attempts` — verify join, ordering, limit
- `get_all_attempts_by_segment` — verify grouping by segment_id

### 3C. `practice.py` Gap Coverage

Add to existing `tests/test_practice.py`:
- `run_loop` start/stop lifecycle (lines 142-152)
- `on_attempt` callback fires on result
- Disconnect during wait (tcp.is_connected goes False)
- Overlay label auto-generation (lines 80-83)

---

## Out of Scope

- Full emulator-based integration tests (no controller available today)
- Upgrading test_practice to real DB+TCP (follow-up session)
- CLI test expansion (not currently used)

## Execution Order

1. Refactors first (1A → 1B → 1C → 1D) — each is small and independently verifiable
2. Outside-view sanity tests — run against current code to see what breaks
3. SSE + db/attempts + practice.py tests — fill coverage gaps
4. Final green suite confirmation
