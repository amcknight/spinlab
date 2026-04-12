# Replay Fixture & Integration Test

## Goal

Create a two-level TAS recording fixture for Love Yourself that can:
1. Rebuild reference runs via replay
2. Serve as a fixture in a full-stack integration test (headless Mesen + dashboard + DB)

## Fixture Files

Checked into `tests/fixtures/love_yourself/`:

| File | Description | Size |
|------|-------------|------|
| `two_level.mss` | Frame-0 save state captured at reference start | ~154 KB |
| `two_level.spinrec` | Input tape: 6255 frames (~104s at 60fps) | ~12 KB |

Source recording: `data/bd94dbb29012c7f5/rec/live_af4ecb2f.*` from a manual reference run on 2026-04-11. Covers two Love Yourself levels with deaths and checkpoint transitions.

These are SpinLab's own binary formats, not ROM content. `.gitattributes` marks them as binary.

## Integration Test

New file: `tests/integration/test_replay_fixture.py`

Marker: `@pytest.mark.emulator` (requires Mesen + Love Yourself ROM from config).

### Flow

1. Uses existing `smoke_mesen_process` + `dashboard_server` fixture pattern from `conftest.py`
2. Copies fixture `.mss` + `.spinrec` to the test's temp data dir
3. POSTs `/api/reference/start` to enter reference mode
4. Sends replay command with fixture files (speed=0 for uncapped)
5. Polls `/api/state` until mode returns to IDLE (replay finished)

### Assertions

- 4 segments detected (2 levels × entrance→checkpoint + checkpoint→exit)
- Save states captured for each waypoint
- Attempts recorded in the DB
- Mode returns to IDLE after replay completes
- `RecSavedEvent` fires (new spinrec written from the replay itself)

### Test ROM Discovery

The test needs the Love Yourself ROM. Discovery order:
1. `SPINLAB_TEST_ROM` env var (if it points to Love Yourself)
2. Config `rom.dir` — look for `Love Yourself.smc`
3. Skip if not found

This is stricter than existing emulator tests which use any ROM in the directory. The fixture was recorded against Love Yourself specifically, so replay only works with that ROM.

## Bug Fixes (already applied)

These were discovered and fixed during this design session:

1. **`vite.py` — IPv6 port check**: `wait_for_port` only checked `127.0.0.1`. Vite binds `[::1]` on some machines, causing the dashboard backend to never start while leaving an orphaned Vite process. Now checks both IPv4 and IPv6.

2. **`capture_controller.py` — absolute recording path**: Recording path was relative, but Lua's `io.open` resolves relative to Mesen's CWD (not the project root). Recordings silently failed to write. Now uses `.resolve()` for an absolute path.

3. **`dashboard.py` — global exception handler**: Unhandled exceptions in routes returned 500 to the client but were not logged anywhere (uvicorn stderr only). Added a catch-all handler that logs to `spinlab.log` with full tracebacks.

## Recording a New Fixture

To create a replay fixture for a different game (or re-record for Love Yourself):

1. Open the ROM in Mesen with SpinLab connected
2. Navigate to your desired starting point (title screen, overworld, level entrance — wherever you want the recording to begin)
3. Click **Start Reference Run** in the dashboard
4. Play through the levels you want in the fixture
5. Click **Stop Reference Run**
6. Find the recording in `data/<game_id>/rec/` — there will be a `.spinrec` and matching `.mss` file
7. Copy both files into `tests/fixtures/<game_name>/` and rename them descriptively (e.g. `two_level.spinrec`, `two_level.mss`)
8. Update `.gitattributes` if adding a new fixture directory
9. The integration test needs the same ROM available in `config.rom_dir` at test time — update the test's ROM discovery if the filename differs

The `.mss` is captured automatically at the exact frame recording begins (frame-synchronized with the `.spinrec`). No separate save state step needed.

## Not In Scope

- No new recording infrastructure — existing reference + replay pipeline is sufficient
- No ROM checked into the repo — test discovers from config, skips if unavailable
- No synthetic/mock version — the point is real emulator end-to-end
- No multi-ROM stress testing (future work with weirder hacks)
