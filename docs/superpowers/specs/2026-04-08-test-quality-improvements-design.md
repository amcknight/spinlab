# Test Quality Improvements: Dedicated Coverage & Gap-Filling Tests

**Date:** 2026-04-08
**Status:** Approved (revised 2026-04-10)

## Problem

SpinLab has 88% overall unit test coverage, but much of it is incidental. Tests
that target one module happen to execute code paths in other modules, inflating
the coverage number and hiding real gaps. For example:

- `capture_controller.py` shows 92% covered, but **0%** comes from dedicated tests
- `session_manager.py` shows 77% covered, but only **61%** is from its own tests
- `scheduler.py` shows 92% covered, but only **38%** is dedicated

The async coordination paths (start/stop practice, disconnect handling, mode
transitions) have essentially zero intentional unit test coverage.

## Solution

Two deliverables. They are independent — either alone is useful — but they
reinforce each other: the script surfaces gaps, the tests close the most
dangerous ones.

### 1. Dedicated Coverage Script

A single Python script, `scripts/dedicated_coverage.py`, that runs the fast
test suite with `--cov-context=test`, then queries the `.coverage` SQLite
database to produce a per-module "honest coverage" table. No bash wrapper —
`python scripts/dedicated_coverage.py` is the invocation.

**Scope:** Fast tests only. Emulator/integration tests are inherently
cross-cutting and their coverage is intentionally broad — labeling it
"incidental" would be misleading.

**Dedication rule (convention, not config):** A test is "dedicated" to module
`spinlab/foo.py` iff it lives in `tests/test_foo.py`. One rule, no mapping
table, no configuration. If `test_foo.py` does not exist, the module reports
"no dedicated tests" and its dedicated coverage is 0%.

This convention is self-documenting: renaming a test file or a module breaks
the link visibly (the dedicated column drops), instead of silently diverging
from a hand-maintained dict. New modules automatically get checked without
editing the script.

**Output format:**

```
Module                    Dedicated  Suite-wide  Gap
session_manager               61%        77%     16%
capture_controller              0%        92%     92%
state_builder                  38%        82%     44%
scheduler                      38%        92%     54%
tcp_manager                    56%        85%     29%
practice                       80%        83%      3%
speed_run                      89%        92%      3%
```

**Implementation sketch:**

- Runs `pytest tests/ --cov=spinlab --cov-context=test` with fast markers only
- Enumerates `python/spinlab/**/*.py`
- For each module `foo.py`, checks whether `tests/test_foo.py` exists. If yes,
  queries the `.coverage` SQLite DB to count lines covered where the context
  string starts with `tests/test_foo.py::`. If no, reports 0% dedicated.
- Prints the table to stdout sorted by gap, descending.

### 2. Gap-Filling Tests

New and expanded test files targeting the five modules with the worst
dedicated-to-suite-wide coverage ratios.

**Cross-cutting rules for this work:**

- **Don't duplicate integration coverage.** If `dashboard_integration` already
  exercises a branch end-to-end through the HTTP layer, don't write a
  duplicate unit test for the same branch. Focus on branches the integration
  tests don't hit.
- **Prefer real collaborators over mocks when cheap.** For CaptureController
  and SessionManager tests, use a real in-memory SQLite `Database` (cheap) with
  a fake `TcpManager`. Pure-mock tests of a thin orchestrator often degenerate
  into "verify the mock was called" tautologies rather than behavior checks.
- **Short timeouts in async tests.** The production `PRACTICE_STOP_TIMEOUT_S`
  is 5 seconds. Tests that exercise the cancel-with-timeout paths should
  inject or monkey-patch the timeout to ~100ms to keep the fast suite fast and
  prevent flakes.

The per-module lists below describe **behaviors to cover**, not exact test
function names. The implementation plan will decide how to group them into
tests.

#### `test_capture_controller.py` (new) — currently 0% dedicated

CaptureController orchestrates reference/replay/fill-gap flows. All its
current coverage is incidental from draft lifecycle, session manager, and
dashboard integration tests.

Behaviors to cover:
- `start_reference` guard rails: returns correct error for draft pending,
  practice active, already replaying, not connected. Happy path sends TCP
  command and transitions to REFERENCE mode.
- `stop_reference` enters draft from captured segments and clears state.
- `start_replay` / `stop_replay` guard rails, plus the "no segments captured →
  hard delete capture run" branch of `stop_replay`.
- `handle_replay_error` with segments (enters draft) vs without (deletes run).
- `handle_disconnect` with segments vs without.
- `start_fill_gap` happy path (hot state loaded) and no-hot-variant error.

Testing approach: real in-memory DB (cheap), fake TcpManager that records
sent commands. This avoids the "testing the mock" trap and exercises the
controller's real interaction with the DB schema.

#### `test_session_manager.py` additions — currently 61% dedicated

Existing tests cover event routing and mode guards well. Missing: lifecycle
coordination.

Behaviors to cover:
- `start_practice` / `stop_practice`: creates PracticeSession, wires async
  task, stop cancels with short timeout.
- `start_speed_run` / `stop_speed_run`: same pattern.
- `on_disconnect`: stops practice, stops speed run, clears cold fill, enters
  draft if segments captured.
- `shutdown`: calls stop_practice + stop_speed_run + tcp.disconnect.

Testing approach: real in-memory DB, fake TcpManager, patched
`PRACTICE_STOP_TIMEOUT_S` to ~100ms. The cancel-with-timeout path is the
trickiest — it needs a PracticeSession that won't exit its loop so the timeout
actually fires, then a verification that the task was cancelled cleanly.

#### `test_state_builder.py` (new) — currently 38% dedicated

StateBuilder constructs the API/SSE state snapshot. The practice branch is
already covered end-to-end by `test_dashboard_integration.py`, so skip it.

Behaviors to cover (only ones not already exercised by dashboard_integration):
- Speed-run branch of `build()`: mode=SPEED_RUN populates current level info.
- Cold-fill branch: mode=COLD_FILL includes cold_fill state.
- Draft state: included when draft is active.
- Idle/no-game base case: mode=IDLE, `game_id=None` returns bare state.

#### `test_scheduler_kalman.py` additions — currently 38% dedicated

Behaviors to cover:
- `_sync_config_from_db`: allocator weights or estimator changed between picks
  is detected and applied.
- `rebuild_all_states`: re-derives all model states from attempt history.
- `set_allocator_weights`: validation (sum != 100 raises, unknown allocator
  name raises).

#### `test_tcp_manager.py` additions — currently 56% dedicated

Behaviors to cover:
- `_read_loop` handling of non-JSON lines: `ok:`, `pong`, unexpected strings.
- `_read_loop` handling of connection closed by remote.
- `disconnect` cancels read task, drains queue, cleans up writer.

## What This Does NOT Include

- **CI integration.** This is a local/personal project; a coverage ratchet in
  CI would be over-engineering.
- **Changes to emulator or smoke tests.** Those are intentionally cross-cutting.
- **Refactoring production code.** The tests target existing code as-is.
- **Coverage threshold enforcement.** The script is a visibility tool, not a gate.

## Testing the Tests

After implementation, run `python scripts/dedicated_coverage.py` and verify
that the dedicated coverage numbers have improved for all five target modules.
The gap column should shrink meaningfully (target: each module below 20% gap,
though this is a guideline, not a gate).

Run `pytest` (full suite) to confirm nothing regresses.
