# Slot Management — Frank Notes

This document is a deliberately frank account of how SpinLab currently handles RetroArch's slot-numbered file conventions for both savestates (`<game>.state<N>`) and movies (`<game>.replay<N>`). **None of this is set up the way it should be.** The current code works (mostly) but is full of compromises, races, and assumptions that bite in real use. If you are reading this to fix something, assume the entire thing is suspect, not just the part you noticed.

There are no plans to do a proper redesign in this branch. The migration's current goal is "working even if a little architecturally bad" (Andrew, 2026-05-08). Treat this doc as both a map of the existing mess AND a punch list for the future cleanup.

## The fundamental problem

RetroArch's NCI surface for state and movie I/O speaks in **slots**, not paths:

- `SAVE_STATE` — saves to `<savestate_directory>/<game>.state<state_slot>` where `state_slot` is RA's *runtime* counter (which auto-indexes on save when `savestate_auto_index = "true"`).
- `LOAD_STATE_SLOT N` — loads from `<game>.state<N>`.
- `RECORD_REPLAY` — records to `<savestate_directory>/<core>/<game>.replay<replay_slot>`. Auto-indexes too. Note: this lands in a **per-core subdirectory** while `.state` files do not.
- `PLAY_REPLAY` — plays back `<game>.replay<replay_slot>` for the current runtime slot.
- `HALT_REPLAY` — stops record OR playback.

There is **no NCI command** that takes a path argument. SpinLab needs to associate state and movie files with logical objects (segments, references) — not arbitrary integer slots that change every session. The mismatch is inherent.

## The shared workaround pattern

For both states and movies, SpinLab's strategy is the same:

1. **Filesystem shuffle.** Trigger RA to write a file via slot. Let RA name it whatever it wants. Snapshot the directory (path → mtime) before. After the operation, find the file that's new or has a newer mtime. Move it to a SpinLab-keyed path.
2. **For load/playback:** copy our SpinLab-keyed file BACK into the slot RA expects, then fire the slot-targeted command.

This is hacky in roughly six ways. They compound. The state side is explored and patched; the replay side is newly implemented and still has known holes. They are documented separately below because the bugs differ.

## Savestate slot management — current state

**File:** `python/spinlab/retroarch/state_io.py`

**Strategy:** "Filesystem shuffle with a reserved slot" — Decision 1 / Decision 6 from the Phase D plan.

- **Save path:** snapshot `<game>.state*` glob → `SAVE_STATE` (writes to RA's auto-indexed slot, NOT a slot we control) → poll for the new/changed file → move it to `<spinlab_state_dir>/<segment_filename>`. Retried 3× with timeouts because RA's `SAVE_STATE` intermittently no-ops during transitions.
- **Load path:** copy SpinLab's segment file → `<game>.state<reserved_slot>` (slot 9999 by default) → `LOAD_STATE_SLOT 9999`. The reserved slot is high enough that the user's manual saves aren't likely to collide.

### Known issues with the savestate side

- **The user's `state_slot` counter advances by one per capture.** Every reference run, every cold-fill, every save bumps the user's slot counter visible in the RA UI. Documented as a Phase D Decision 1 tradeoff. Annoying but not broken.
- **Move retries on Windows.** RA holds the file open for ~hundreds of ms after writing. `shutil.move` raises `PermissionError`. We retry (5×, 200ms each). The fallback is `shutil.copyfile` + leave the source for "future cleanup" which never actually runs — it's a slow leak in the user's RA savestate dir.
- **Reserved slot file leaks.** A hard-killed dashboard leaves `<game>.state9999` sitting in RA's dir. We sweep on `__init__` (`_cleanup_stale_slot_file`), but only for the current basename — files from other ROMs persist.
- **Phase 0 INCONCLUSIVE finding.** The Phase 0 spike couldn't get NCI `SAVE_STATE` to work at all. The fix (`run_ahead_secondary_instance = "true"`) was discovered live during F-live debugging. Without that cfg, single-instance runahead corrupts the state buffer; SAVE_STATE silently produces wrong/missing output. README documents this; new users hitting it will not connect the dots without reading the README.
- **Cheevos hardcore mode silently disables NCI savestate commands.** Documented in the README. Cheevos hardcore is unrelated to runahead but produces the same surface symptom (commands appear to fire but no file appears). This is two completely different reasons savestates can fail silently, depending on cfg.
- **Race during the file-discovery poll.** If two saves fire close together (shouldn't happen in normal flow but does during debugging), the second one's "new file" detection picks up the FIRST save's file. There's no per-call serialization.

## Movie slot management — current state (worse)

**File:** `python/spinlab/retroarch/movie.py` + `python/spinlab/retroarch/orchestrator.py::_on_replay`

**Strategy:** Same filesystem-shuffle pattern as savestates, but **less proven and currently broken in ways we have observed but not fully diagnosed.**

### Recording (works mostly)

- Snapshot existing `<movie_dir>/*` (any extension — `iterdir` set-diff, not glob) with mtimes.
- Send `RECORD_REPLAY` (RA picks the next free slot, e.g. `Toothpaste.replay65`).
- User plays. On `HALT_REPLAY`:
  - Poll for any file whose mtime advanced or which is new since baseline.
  - On Windows, RA holds the file open briefly — copy + retry-unlink instead of move.
  - Move the discovered file to `<data_dir>/<game_id>/rec/<ref_id>.replay`.

#### Known recording issues

- **`replay_max_keep = "0"` (the RA default) silently blocks new recordings.** When existing `.replay<N>` files for the loaded game exist, RA refuses to write new ones — `RECORD_REPLAY` no-ops, no file appears, our discovery scan times out. README now requires `replay_max_keep = "99"`. Future users will hit the original failure with the unhelpful "no new file appeared" error message; we added a hint that names this exact config flag, but it's still a config-edit-and-restart loop.
- **In-place rewrites.** RA sometimes overwrites the same `.replay<N>` filename across recordings rather than incrementing. Our baseline tracks mtimes, not just names, so we catch this. (See `94801f5` for the bug + fix.)
- **`movie_dir` derivation.** Movie files land in `<savestate_directory>/<core_name>/`, NOT in `<savestate_directory>` directly like state files. `EmulatorConfig.ra_movie_dir` is an explicit override; otherwise we derive `<emu.savestate_dir>/<emu.ra_core_subdir>`. We detect when `savestate_dir` already ends with `ra_core_subdir` and skip the second append, because some users (e.g. Andrew) point `savestate_dir` at the per-core path directly. **This is two configs whose interaction must be reasoned about, with a defensive `name == subdir` check making the wrong combination silently DTRT.** A clean redesign would have `EmulatorConfig` expose `movie_dir` directly and stop deriving.

### Playback (broken: RA refuses our staged file)

- We have a SpinLab-keyed `.replay` file at `<data_dir>/<game_id>/rec/<ref_id>.replay`.
- Stage: copy that file to `<movie_dir>/<game_basename>.replay0` (slot 0, hardcoded — see below).
- Send `PLAY_REPLAY`. RA looks for `<game_basename>.replay<runtime_slot>` and either loads it or pops "Failed to load movie file" in-app (invisible over NCI).
- We verify by reading 16 WRAM bytes, sleeping 150ms, re-reading. If unchanged: emit `ReplayErrorEvent`, stop player. **Verification works.** What does not work is making RA actually load our file in the first place.

#### Why playback is broken (observed 2026-05-08)

1. **Slot 0 is a guess, not a derivation.** We hardcode `staged_slot=0` matching the typical `replay_slot = "0"` cfg, but RA's *runtime* slot may differ — `replay_auto_index = "true"` shifts it as recordings happen, AND **persists per-game across sessions**. Andrew's 2026-05-08 log evidence: cfg said `replay_slot = "0"` but RA's startup log said `[Replay] Found last replay slot: #64`, meaning the runtime slot was 64. Where the persistence lives is not yet identified — not in the cfg, not in the per-game `.lrtl` runtime-tracker file (which only persists `state_slot`). It must be in some RA-internal file we haven't traced. There is no NCI command to read the runtime slot; `GET_CONFIG_PARAM replay_slot` returns the cfg value, not the runtime value.

   **Mitigation in current code (2026-05-08):** the orchestrator reads RA's most recent log file (`<retroarch_path.parent>/logs/retroarch__*.log`) and parses for the latest `[Replay] Replay slot: N` or `[Replay] Found last replay slot: #N` line. The matched slot is used as `staged_slot` for `MoviePlayer.play`. If the log isn't enabled or unparseable, fall back to slot 0.

   This is **fragile and tightly coupled to RA's log format** — if RA changes the log message wording in a future version, our parser breaks silently and replay falls back to slot 0 which won't match. The README documents `log_to_file = "true"` as required cfg.

   **Why we don't navigate via SLOT_MINUS:** RA's NCI `REPLAY_SLOT_MINUS` does exist and works, but RA's input layer debounces hotkey-style commands at ~6Hz. 2026-05-08 evidence: 200 fires at 16ms spacing only landed 20 decrements. To converge from slot 64 to 0 reliably, we'd need ~64 fires at 200ms = 13s of lag per replay. Log parsing is faster and more reliable.

   **A clean redesign** would either disable `replay_auto_index` entirely (and accept that user manual recordings then overwrite slot 0), or use `--bsvplay <path>` at RA launch. Both avoid the slot guessing entirely.
2. **Maybe RA caches the directory listing at startup.** Our `<game>.replay0` file did not exist when RA started. We wrote it mid-session. There is some evidence (not proven) that RA scans replay slots at startup and refuses files that appeared after, even if the path matches the slot it would load. Confirming this requires reading RA's source.
3. **`HALT_REPLAY` may not fully reset playback state between attempts.** Repeated play/stop cycles in one session may leave RA in a state where subsequent `PLAY_REPLAY` calls behave differently than the first.
4. **`PLAY_REPLAY` is fire-and-forget.** We have no acknowledgement that RA processed the command, only the in-app popup which is invisible over NCI. The 150ms verification sleep is empirically picked, not principled.
5. **The file path includes a string ROM name (`<game_basename>`) that came from RA's `GET_STATUS`.** If `GET_STATUS` returns a slightly different name than what RA uses for replay file naming (e.g. encoding edge cases, Unicode normalization), the staged path doesn't match what RA looks up. We've not seen this fail yet but the contract is fragile.

#### What we did briefly do that "worked"

The first version of MoviePlayer staged as `<movie_dir>/spinlab_movie.replay` — a fixed name independent of the loaded ROM. The determinism smoke test passed under that scheme. **We do not understand why** — RA shouldn't have loaded that filename. Possibilities:
- RA was running normally (no replay loaded) and the test measured "RA from cold start to +3s of arbitrary play" which happens to be deterministic without any actual playback. **This means the determinism test may be a false positive** — see `tests/integration/test_movie_smoke.py::test_movie_playback_deterministic` for the actual implementation. If you're going to trust that test as evidence "movie playback is deterministic," verify by changing the source `.replay` file to a different recording and confirming the byte differs across the two recordings, not just across two runs of the same recording.
- RA may have had some "find any compatible movie file" fallback that loaded `spinlab_movie.replay` because its embedded ROM checksum matched. Believable but unverified.

This is the most concerning gap in the documentation. **The smoke tests claim to validate playback determinism. They may not actually be running our movie file.** Treat the determinism claim with suspicion until confirmed by a test that loads two different `.replay` files and asserts the memory byte differs.

#### Verification heuristic — also imperfect

The 16-byte WRAM-advance check after `PLAY_REPLAY` catches the obvious "RA didn't start playing" case. It does NOT catch:
- "RA started playing but with an arbitrary timeline" (e.g. RA loaded a different `.replay` file from a different recording for the same ROM)
- "RA started playing the wrong file but the byte just happened to advance the same way during the 150ms window"
- "RA is in playback but at a stalled frame for unrelated reasons"

A proper verification would compare playback against the recorded movie's expected memory state at a known frame. We don't do that.

## SAVE_STATE during BSV recording — fixed locally (2026-05-09)

> **2026-05-09 update:** the conclusion below ("hard constraint", `bsv_movie_write_checkpoint` is the failing function, `replay_checkpoint_interval` matters) is **wrong on the mechanism**. The bug is real but it's in `replay_get_serialized_data` ([`input/bsv/bsvmovie.c:1121`](https://github.com/libretro/RetroArch/blob/v1.22.2/input/bsv/bsvmovie.c#L1121)), not the checkpoint writer. Root cause is C99/C11 §7.21.5.3: streams opened in update mode (`r+`/`w+`) require a positioning call between input and output. RA's `replay_get_serialized_data` does an `intfstream_rewind` + `intfstream_read` to embed the in-progress recording into the `.state` file, then returns without seeking — and every subsequent `intfstream_write` from `bsv_movie_next_frame` silently no-ops on Windows MSVCRT. The `frame_counter` keeps advancing, so end-of-recording header writes a frame count that doesn't match the actual on-disk content. **Fixed on a vendored RA build** with one line — `intfstream_seek(handle->file, file_end, SEEK_SET);` after the read. Upstream PR pending.
>
> **Full investigation log:** [`upstream-fix-findings-2026-05-09.md`](upstream-fix-findings-2026-05-09.md). The original analysis below is preserved as a record of what was thought at the time but should not be trusted as a description of the bug.

This was the longest single thread of debugging in Phase E. **Conclusion: with snes9x_libretro and bsnes_libretro on RA 1.22.2, you cannot fire SAVE_STATE during a BSV recording session without breaking the recording.**

Empirical evidence (2026-05-08, confirmed via direct NCI probes — no SpinLab in the loop):

| `replay_checkpoint_interval` | Result with 3 SAVE_STATE calls during 20s recording |
|---|---|
| `"0"` (default) | Playback EOFs at first SAVE_STATE point. RA log shows clean `Stopping movie record`. The .replay file's input track is truncated to pre-first-save inputs. |
| `"60"` | Same as `"0"`. No checkpoint fires within the 20s window because interval is in seconds, not frames. |
| `"1"` | RA log shows `[ERROR] [Replay] failed to write checkpoint, exiting record` after the first SAVE_STATE. Recording silently ends; subsequent SAVE_STATE calls go through but BSV is already done. |

> *Note on the table above:* the row labeled `"1"` was either misobserved or was testing under different conditions — the actual bug doesn't fire `bsv_movie_write_checkpoint` and so wouldn't produce that error log. Treat that row with suspicion.

The relevant RA source ~~is `input/bsv/bsvmovie.c` `bsv_movie_write_checkpoint()`: SAVE_STATE during a recording sets `BSV_FLAG_MOVIE_FORCE_CHECKPOINT`, which on the next frame triggers `core_serialize() → encode (RAW or STATESTREAM) → compress (NONE/ZLIB/ZSTD) → file write`. If any step returns -1, RA logs the error and ends the recording.~~ **(retracted: SAVE_STATE does NOT set BSV_FLAG_MOVIE_FORCE_CHECKPOINT — that flag is set only by the separate `SAVE_REPLAY_CHECKPOINT` NCI command. SAVE_STATE goes through `replay_get_serialized_data` instead. See findings doc.)**

**Tested cores:**
- `snes9x_libretro.dll` — fails as above
- `bsnes_libretro.dll` — same pause-on-save behavior; checkpoint write also fails

**Note on bsnes:** RA also logs `[WARN] [Run-Ahead] Run-Ahead unavailable because this core lacks deterministic save state support` for bsnes. So bsnes can't be used for production anyway (Andrew needs runahead for low-latency live play). Bsnes was tested as the "BSV is bsnes-native, should work better" hypothesis; it doesn't.

### Background reading

- [Issue #14886 (closed): Re-recording support in savestates](https://github.com/libretro/RetroArch/issues/14886) — the feature request
- [PR #15070 (merged 2023-03-07): Associate states with replays](https://github.com/libretro/RetroArch/pull/15070) — the implementation that *should* enable mid-record SAVE_STATE
- [Issue #15806 (open): My request to fix RetroArch Replay](https://github.com/libretro/RetroArch/issues/15806) — community report that `replay_checkpoint_interval = "1"` is the only working setting; we can't reproduce that working
- [PR #17042 (merged 2024-10-04): Replay format improvements](https://github.com/libretro/RetroArch/pull/17042) — version-bump to v2 BSV
- `input/bsv/bsvmovie.c` in libretro/RetroArch source — the actual code

### Workarounds we considered

> **Resolved 2026-05-09:** option (4) shipped. Workarounds (1)-(3) below are no longer needed. The patched RA build does the right thing.

1. ~~**Decouple — disable BSV recording during reference runs.**~~ No longer the de facto state once the patched RA is deployed.
2. ~~**Multi-segment recording (HALT → SAVE → RECORD per segment).**~~ Not needed.
3. ~~**Switch core (bsnes/mesen-s).**~~ Not needed — the bug was in RA's BSV layer, not the core.
4. **Patch RA upstream.** ~~The fix would be to make `bsv_movie_write_checkpoint()` more graceful~~ — actual fix turned out to be a one-liner in `replay_get_serialized_data` adding `intfstream_seek(handle->file, file_end, SEEK_SET)` after the existing `intfstream_read`. See [`upstream-fix-findings-2026-05-09.md`](upstream-fix-findings-2026-05-09.md) for full root cause and PR-ready writeup.

## What "good" would look like

Some of these are big lifts; many would simplify a lot of the above. Listed for the future cleanup pass.

1. **Stop using slot-keyed RA commands at all.** If RA gains an NCI `MOVIE_FILE_LOAD <path>` or `STATE_LOAD_FROM_PATH <path>` (not in 1.22.2), use it. The entire "filesystem-shuffle into a slot" dance goes away.
2. **Use `--bsvplay <path>` at RA launch.** This is the canonical way to play a specific replay file. Trade-off: requires relaunching RA per replay. Could be acceptable for the test path; not great for an interactive replay-during-practice flow.
3. **Use a SpinLab-controlled, isolated `savestate_directory`.** RA writes there; the user's normal RA dir is untouched. SpinLab's slot manipulation doesn't pollute user data and we can reason about contents. The `MEMORY.md` `project_spinlab_ra_isolation` note from 2026-05-08 captures this aspiration.
4. **Track RA's runtime slot deliberately.** Use `STATE_SLOT_PLUS`/`STATE_SLOT_MINUS` and equivalent replay-slot commands (if they exist) to navigate to a known slot before each operation. Stop relying on cfg defaults matching runtime.
5. **Replace the WRAM-advance verification with a content-based check.** Two `.replay` files of the same ROM produce different byte sequences at the same frame. The smoke test should assert THAT, not that two playbacks of the same file produce the same byte (which is true even when no movie is playing).
6. **Surface RA's in-app errors to NCI.** This is on libretro. We can't fix it here. We can document the workaround (enable `log_to_file = "true"` and tail the file) and consider parsing the log file as a transitional measure.

## Test coverage today

- `tests/unit/test_movie_recorder.py` — recorder unit tests, fake NCI, tmp filesystem. Lifecycle, error paths, in-place rewrite detection. ✓
- `tests/unit/test_movie_player.py` — player unit tests, fake NCI. Lifecycle, error paths, state cleanup on NCI raise. ✓
- `tests/unit/test_retroarch_orchestrator.py` — orchestrator wiring, including the verification-failure → ReplayErrorEvent path. ✓
- `tests/integration/test_movie_smoke.py::test_movie_record_toggle_creates_file` — end-to-end record. ✓
- `tests/integration/test_movie_smoke.py::test_movie_playback_deterministic` — claims to validate determinism. **Suspect — see "What we did briefly do that 'worked'" above.** ⚠
- `tests/integration/test_movie_smoke.py::test_poller_runs_during_playback` — xfailed (32Hz vs 54Hz threshold). The polling-during-playback gap is its own follow-up beyond slot management.
- `tests/integration/test_replay_fixture.py` — xfailed. End-to-end replay → segment capture. Will start passing if the slot/staging issue gets resolved AND the polling throttle lands.

The unit tests have good coverage of what the code actually does. The integration tests prove less than they claim.
