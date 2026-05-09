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

1. **Slot 0 is a guess, not a derivation.** We hardcode `staged_slot=0` matching the typical `replay_slot = "0"` cfg, but RA's *runtime* slot may differ — `replay_auto_index = "true"` shifts it as recordings happen. There is no NCI command to read the runtime slot; `GET_CONFIG_PARAM replay_slot` returns the cfg value, not the runtime value. We have no way to query "what slot will the next PLAY_REPLAY look at?"
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
