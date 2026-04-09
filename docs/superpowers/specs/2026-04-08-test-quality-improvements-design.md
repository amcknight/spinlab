# Test Quality Improvements: Dedicated Coverage & Gap-Filling Tests

**Date:** 2026-04-08
**Status:** Approved

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

Two deliverables:

### 1. Dedicated Coverage Script

A new `scripts/dedicated_coverage.sh` that runs the fast test suite once with
`--cov-context=test`, then queries the `.coverage` SQLite database to produce a
per-module "honest coverage" table.

**Scope:** Fast tests only. Emulator/integration tests are inherently
cross-cutting and their coverage is intentionally broad — labeling it
"incidental" would be misleading.

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

**Implementation:**

- Runs `pytest tests/ --cov=spinlab --cov-context=test` (fast markers only)
- A Python helper script queries the `.coverage` SQLite DB to count lines
  covered per (module, context-regex) pair
- Module-to-test-prefix mappings defined in the helper (e.g.,
  `session_manager -> test_session_manager`)
- Prints the table to stdout

### 2. Gap-Filling Tests

New and expanded test files targeting the five modules with the worst
dedicated-to-suite-wide coverage ratios.

#### `test_capture_controller.py` (new) — currently 0% dedicated

CaptureController orchestrates reference/replay/fill-gap flows. All its current
coverage is incidental from draft lifecycle, session manager, and dashboard
integration tests.

Tests:
- `start_reference` guard rails: draft pending, practice active, already
  replaying, not connected. Happy path sends TCP command, returns REFERENCE mode.
- `stop_reference`: enters draft from captured segments, clears state.
- `start_replay` / `stop_replay`: guard rails plus "no segments captured -> hard
  delete capture run" branch.
- `handle_replay_error`: with segments (enters draft) vs without (deletes run).
- `handle_disconnect`: same branching as replay_error.
- `start_fill_gap`: happy path loads hot state; no-hot-variant error case.

#### `test_session_manager.py` additions — currently 61% dedicated

Existing tests cover event routing and mode guards. Missing: lifecycle
coordination.

Tests:
- `start_practice` / `stop_practice`: creates PracticeSession, wires up async
  task, stop cancels with timeout.
- `start_speed_run` / `stop_speed_run`: same pattern.
- `on_disconnect`: stops practice, stops speed run, clears cold fill, enters
  draft if segments captured.
- `shutdown`: calls stop_practice + stop_speed_run + tcp.disconnect.

#### `test_state_builder.py` (new) — currently 38% dedicated

StateBuilder constructs the API/SSE state snapshot. The practice branch is
covered by dashboard_integration, but other branches are not.

Tests:
- Speed-run branch of `build()`: mode=SPEED_RUN populates current level info.
- Cold-fill branch: mode=COLD_FILL includes cold_fill state.
- Draft state: included when draft is active.

#### `test_scheduler_kalman.py` additions — currently 38% dedicated

Tests:
- `_sync_config_from_db`: allocator weights or estimator changed between picks.
- `rebuild_all_states`: re-derives all model states from attempt history.
- `set_allocator_weights`: validation (sum != 100, unknown allocator name).

#### `test_tcp_manager.py` additions — currently 56% dedicated

Tests:
- `_read_loop`: non-JSON lines (ok:, pong, unexpected), connection closed by
  remote.
- `disconnect`: cancels read task, drains queue, cleans up writer.

## What This Does NOT Include

- **CI integration.** This is a local/personal project; a coverage ratchet in CI
  would be over-engineering.
- **Changes to emulator or smoke tests.** Those are intentionally cross-cutting.
- **Refactoring production code.** The tests target existing code as-is.
- **Coverage threshold enforcement.** The script is a visibility tool, not a gate.

## Testing the Tests

After implementation, run `scripts/dedicated_coverage.sh` and verify that the
dedicated coverage numbers have improved for all five target modules. The gap
column should shrink significantly.

Run `pytest` (full suite) to confirm nothing regresses.
