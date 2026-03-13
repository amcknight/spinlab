# Multi-Game Support

## Problem

SpinLab currently supports one game, hardcoded in config.yaml. Andrew plays ~300 SMW romhacks and wants to open any ROM in Mesen and have SpinLab automatically track it — no pre-configuration needed.

## Design

### Identity & Discovery

- **Game ID = ROM checksum**, truncated SHA-256 (first 16 hex chars). Stable across file renames. Collision probability across 300 ROMs is negligible.
- **Display name = ROM filename** (sans extension), auto-set on first detection.
- **Auto-creation:** First time a checksum is seen, a new game record is created in the DB. No manual setup.
- **Rename handling:** If the same checksum appears with a different filename, the display name is NOT auto-updated (user may have renamed it in the dashboard). User can rename via the Config tab.

### Checksum Computation

Lua sends the ROM filename over TCP. Python computes the checksum by reading the ROM file from disk. This keeps Lua lightweight — no hashing on the emulator thread.

- Config provides the ROM directory path (`rom.dir`, e.g. `C:\Users\thedo\Dropbox\SNES\SMW Hacks`).
- Dashboard receives filename from Lua, resolves to full path via `rom.dir`, computes SHA-256, truncates to 16 hex chars.
- If the ROM file can't be found on disk (e.g. loaded from a different directory), fall back to using the filename as a temporary game ID and log a warning.

### Game Context Flow

1. Lua script reads ROM filename from Mesen API on startup/ROM load.
2. Lua sends `{"event": "rom_info", "filename": "City of Dreams.sfc"}` over TCP.
3. Dashboard resolves file path, computes checksum, looks up game in DB.
4. Found → sets active `_game_id`, loads that game's splits/model/sessions.
5. Not found → creates new game record (using `INSERT ... ON CONFLICT(id) DO NOTHING` to preserve existing display names), sets as active. No splits yet — ready for first reference run.
6. Header bar updates to show game name.

### Mode Transitions on Game Switch

When the dashboard detects a different ROM than the current active game:

- **Any mode (idle, reference, practice):** Stop active session, clear all in-flight state (scheduler, practice session, reference capture), switch to idle for the new game.
- **Scheduler invalidation:** `_scheduler[0]` is set to `None` and lazily re-created for the new game on next use.
- **Practice session cleanup:** If running, `is_running` is set to False, session stats are saved before switching.

### Dashboard State Management

`game_id` changes from a closure variable to a mutable container, consistent with existing patterns:

```python
_game_id: list[str | None] = [None]  # set by rom_info event
_game_name: list[str | None] = [None]
```

All endpoints that reference `game_id` read from `_game_id[0]`. Endpoints return an error if `_game_id[0] is None` (no ROM loaded yet).

### Dashboard UI

**Header bar:** Game name appears next to "SpinLab" — always visible across all tabs.

```
SpinLab  ·  City of Dreams                    ● Connected
```

When no ROM is loaded / emulator disconnected, the game name is absent.

### State File Organization

Save states are organized into per-game subdirectories to keep the filesystem manageable with 300+ games:

```
LuaScriptData/spinlab/states/
├── a1b2c3d4e5f67890/     # checksum-based subdirectory
│   ├── 44_8.mss
│   └── 105_3.mss
├── f0e1d2c3b4a59876/
│   └── 12_1.mss
```

Lua receives the game ID (checksum) from Python via TCP and uses it for the subdirectory name. The level/room portion of the filename no longer needs the game prefix.

### IPC: New `rom_info` Event

Added to the TCP protocol. Lua sends this on connect and whenever the ROM changes:

```json
{"event": "rom_info", "filename": "City of Dreams.sfc"}
```

Dashboard responds with the resolved game ID so Lua can use it for state file paths:

```json
{"event": "game_context", "game_id": "a1b2c3d4e5f67890", "game_name": "City of Dreams"}
```

### Config Changes

```yaml
# REMOVED:
# game:
#   id: smw_cod
#   name: "SMW: City of Dreams"

# NEW:
rom:
  dir: "C:/Users/thedo/Dropbox/SNES/SMW Hacks"  # where ROMs live

# KEPT:
game:
  category: "any%"  # default category for new games
```

### What Changes

| Component | Change |
|-----------|--------|
| **Lua script** | Send ROM filename over TCP on connect. Remove hardcoded `GAME_ID`. Receive `game_context` response for state file subdirectory. |
| **Dashboard** | Replace fixed `game_id` with mutable `_game_id`/`_game_name`. Handle `rom_info` events. Compute checksum from ROM file. Update `/api/state` to include `game_name`. |
| **DB** | Change `upsert_game` to use `INSERT ... ON CONFLICT DO NOTHING` for the name field (preserve user renames). No schema changes. |
| **Config** | Remove `game.id` and `game.name`. Add `rom.dir`. Keep `game.category` as default. |
| **CLI** | `create_app()` no longer takes `game_id`. Dashboard starts in "no game" state. |
| **Header UI** | Add game name display next to "SpinLab". |
| **Split IDs** | Format stays `game_id:level:room:goal` — `game_id` becomes the truncated checksum. |
| **Save states** | Organized into per-game subdirectories by checksum. |
| **Reset endpoint** | Scoped to current game only (don't nuke all games' data). Attempts deleted via subquery through splits: `DELETE FROM attempts WHERE split_id IN (SELECT id FROM splits WHERE game_id = ?)`. Same pattern for model_state. Sessions deleted directly via `game_id` FK. |

### What Doesn't Change

- Memory addresses in Lua script (all SMW romhacks use the same engine)
- Scheduler, Kalman estimator, allocators (allocator config is global, not per-game)
- DB schema (already designed for multi-game)
- Split ID structure (just the game_id portion changes)
- Sessions, attempts, model_state tables — all already FK to game_id
- References (capture_runs) — already FK to game_id

### Data Migration

No migration needed. Existing `smw_cod` data can be discarded. Fresh start with checksum-based IDs.

### Edge Cases

- **Same ROM, two filenames:** Checksum matches — same game, no duplicate.
- **ROM file renamed:** Checksum unchanged — SpinLab still recognizes the game. Display name preserved.
- **ROM file updated/patched:** New checksum — treated as a new game. Correct since save states from the old version won't work.
- **Emulator disconnects:** Mode → idle, game context cleared, header shows no game name. Reconnect triggers fresh `rom_info`.
- **Dashboard starts before emulator:** No active game until first `rom_info`. UI shows disconnected state, all game-scoped endpoints return error.
- **ROM loaded from outside `rom.dir`:** Fallback to filename-based game ID, log warning. Still functional but won't survive renames.
- **Multiple ROMs with same filename in different subdirectories:** Checksum differentiates them. Display name may collide but game ID won't.

### Mesen API for ROM Filename

Need to verify exact API. Candidates:

- `emu.getRomInfo()` — likely returns table including filename
- `emu.getState()` with ROM info fields

Lua only needs to extract the filename — no hashing required on the Lua side.
