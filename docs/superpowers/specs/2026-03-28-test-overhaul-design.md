# Test Suite Overhaul — Design Spec

## Goal

Make the test suite faster, more valuable, and agent-friendly. Priority order: coverage > clarity > speed.

## 1. Poke Engine Batching (Lua + Python)

### Problem
Each integration test launches a separate Mesen2 process (~5s overhead per test: 2s startup sleep + 3s TIME_WAIT cooldown). 12 tests = ~60s in overhead alone.

### Solution
One Mesen2 launch per pytest session. The poke engine accepts multiple scenarios sequentially over the same TCP connection.

**Protocol:**
1. Python sends `poke_scenario` JSON
2. Engine runs it, fires transition events as usual
3. Engine sends `{"event": "scenario_done"}` sentinel after settle window
4. Engine resets all state (poke + spinlab detection)
5. Python sends next `poke_scenario` (or `quit` to terminate)

**Lua state reset** via `reset_detection_state()` global defined in `spinlab.lua`:
- Zeros `prev` table (all memory addresses)
- Resets `transition_state` (`died_flag`, `cp_ordinal`, `first_cp_entrance`)
- Clears `cp_acquired`, `level_start_frame`, `exit_this_frame`

Poke engine resets its own state: `poke_schedule`, `held_values`, `last_poke_frame`, `scenario_start_frame`, `own_frame_counter` offset.

**Wait time elimination:**
- Remove 2s startup `asyncio.sleep` — TCP connect retry loop is the ready gate
- Remove 3s TIME_WAIT cooldown — session-scoped fixture only tears down once at end
- Keep TCP connect retry loop (already event-driven)

**Pytest fixture changes:**
- `mesen_process`: session-scoped
- `tcp_client`: session-scoped
- `run_scenario`: function-scoped, reuses persistent connection, collects events until `scenario_done` sentinel (not connection close)

## 2. Poke File Cleanup

### Problem
Each `.poke` file is ~50% inline comments explaining the format. The README already documents this.

### Solution
Strip inline comments to a single header line per file: `# scenario_name — expected_event_1, expected_event_2`. Move the "how to read a poke file" documentation to the README (most of it is already there).

### Before
```
# entrance_goal.poke — Level entrance followed by normal goal exit
# Expects: level_entrance, level_exit(goal=normal)
# Held values: each poke persists until overridden (ROM can't overwrite)
settle: 60

# Frame 0: baseline — clear all flags so transitions fire cleanly
0: level_start=0 exit_mode=0 fanfare=0 player_anim=0 io_port=0 midway=0
# Frame 1: set level context
1: game_mode=20 level_num=105 room_num=1
# Frame 2: entrance trigger (level_start 0→1)
2: level_start=1
# Frame 15: goal exit (exit_mode 0→1, fanfare=1)
15: exit_mode=1 fanfare=1
```

### After
```
# entrance_goal — entrance, exit(normal)
settle: 60

0: level_start=0 exit_mode=0 fanfare=0 player_anim=0 io_port=0 midway=0
1: game_mode=20 level_num=105 room_num=1
2: level_start=1
15: exit_mode=1 fanfare=1
```

## 3. New Poke Scenarios

Three new scenarios covering gaps found in `detect_transitions()`:

### `multiple_checkpoints.poke`
Entrance -> midway 0->1 (cp_ordinal=1) -> death -> cold respawn -> midway 0->1 again (cp_ordinal=2).
Tests: cp_ordinal incrementing, second cold capture.

### `death_before_checkpoint.poke`
Entrance -> death -> respawn at level start (no checkpoint hit).
Tests: spawn with `is_cold_cp=false`, `died_flag` set/cleared without checkpoint involvement.

### `boss_defeat.poke`
Entrance -> `boss_defeat` 0->1 + `fanfare` 0->1 on same frame.
Tests: goal="boss" in `detect_finish()`.

## 4. Integration Test Consolidation

### Problem
12 test methods across 6 classes, each calling `run_scenario()` independently. Same scenario runs up to 3 times for separate assertions.

### Solution
One `run_scenario()` call per scenario, all assertions in one test function. 12 methods become ~9 functions (6 existing + 3 new), each running the scenario exactly once. With session-scoped Mesen2, total runtime: one process launch.

## 5. Unit Test Consolidation

### Merge dashboard test files
`test_dashboard.py` + `test_dashboard_integration.py` -> single file. The seeded DB fixture becomes primary. Unique tests from `test_dashboard.py` (no-game-loaded, 503/409 error states) fold in. Overlapping tests deleted.

### Strip `test_session_manager.py`
Currently 37 tests, mostly mock-call assertions. Keep:
- Mode transition guards (can't start practice during reference, etc.)
- Event routing (TCP event -> correct handler)
- Game switching logic

Kill: anything that asserts `mock_db.some_method.assert_called()` when the same behavior is covered by dashboard tests with a real DB. Target: 37 -> ~15.

### Extract shared fixtures to `tests/conftest.py`
- `seeded_db` fixture (game + segments + variants + attempts + model state)
- Segment builder helper
- Mock TCP factory

### Kill mock-wiring-only tests
Across `test_draft_lifecycle.py` and `test_replay.py`, remove tests that mock everything and only assert `mock.called`. Keep tests that verify actual state machine logic.

### Leave alone
Estimator tests, allocator tests, DB tests, poke parser tests, spinrec tests. These are pure-logic unit tests with minimal mocking — exactly the right kind.

## Expected Outcomes

- Integration test runtime: ~7min -> <1min (one Mesen2 launch, ~9 scenario runs)
- Integration coverage: 6 scenarios -> 9 scenarios (boss defeat, multi-checkpoint, death-before-cp)
- Unit test count: ~261 -> ~200 (fewer but more valuable)
- Poke files: clean, scannable, comment-free data
- Agent-friendliness: one obvious place per behavior, real DB over mocks, self-documenting scenarios
