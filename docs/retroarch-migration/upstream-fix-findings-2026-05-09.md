# RetroArch BSV+SAVE_STATE Investigation — Findings Report

**Session:** 2026-05-09. **Status:** **RESOLVED** — one-line fix landed on a vendored RA build. Upstream PR pending.
**Companion to:** [`upstream-fix-plan.md`](upstream-fix-plan.md).

---

## Resolution (added 2026-05-09 evening)

**Root cause:** the C standard's update-mode invariant. From C99/C11 §7.21.5.3 on `fopen`:

> When a file is opened with update mode … input shall not be directly followed by output without an intervening call to a file positioning function, unless the input operation encounters end-of-file.

The BSV recording file is opened with `RETRO_VFS_FILE_ACCESS_WRITE | RETRO_VFS_FILE_ACCESS_READ` ([`tasks/task_movie.c:124-126`](file:///c:/Users/thedo/git/RetroArch/tasks/task_movie.c#L124)) — stdio's `r+`/`w+` mode. After `replay_get_serialized_data`'s `intfstream_read`, the stream is in input mode. The next `intfstream_write` from `bsv_movie_next_frame` is "output following input without intervening positioning call" → undefined behavior → on Windows MSVCRT (and most stdio implementations) this manifests as silent no-op writes. `frame_counter` keeps advancing in memory, but bytes never reach disk. End-of-recording header write claims the full frame count, which is why the file appears valid but plays back truncated.

Latent in PR #15070 since 2023-03 because (a) the save itself succeeds, (b) the BSV header lies about frame_count at recording-stop, (c) no error log fires, (d) only manifests on actual replay-back, and (e) almost nobody in the wild fires SAVE_STATE during a recording session.

**The fix** (one line, `input/bsv/bsvmovie.c`, in `replay_get_serialized_data`, immediately after the existing `intfstream_read(handle->file, buf, file_end)`):

```c
intfstream_seek(handle->file, file_end, SEEK_SET);
```

That positioning call satisfies the C standard requirement. Subsequent `bsv_movie_next_frame` writes proceed normally.

**Verified end-to-end:** patched RA built, deployed to `C:/RetroArch-Win64-fixed/`, and tested on this machine 2026-05-09 evening.

| Test | Stock v1.22.2 | Patched (this PR) |
|---|---|---|
| `count_replay_frames.py` on a 20s recording with 3 saves | header=1201, parsed=**299** (truncated) | header=1201, parsed=**1201** (MATCH) |
| Visual playback via `PLAY_REPLAY` | EOF'd at first save (~5s in) | full 20s played, all inputs honored past every save |

Same probe, same gameplay, same cfg. The only difference is the one-line seek.

**For the upstream PR description**, this is the entire technical story. The investigation log below is preserved for historical/learning value but is no longer needed for the fix itself.

---

## Original investigation log (2026-05-09 morning)

This document records what was empirically established and what was traced through the source. The original plan was written before this investigation; some of its hypotheses turned out to be wrong. Below: what changed, with citations.

---

## TL;DR

The bug exists, but it is **not** in `bsv_movie_write_checkpoint` as `slot-management.md` and the original `upstream-fix-plan.md` claimed. The actual culprit is `replay_get_serialized_data` ([`input/bsv/bsvmovie.c:1121`](file:///c:/Users/thedo/git/RetroArch/input/bsv/bsvmovie.c#L1121)), called from the state-save path at [`tasks/task_save.c:418`](file:///c:/Users/thedo/git/RetroArch/tasks/task_save.c#L418). This function does an `intfstream_rewind` on the in-progress recording file, reads it back to embed in the `.state` file's `RASTATE_REPLAY_BLOCK`, and somehow leaves the BSV file handle in a state where subsequent `bsv_movie_next_frame` writes silently no-op (file unchanged) while `frame_counter` continues to advance.

**Empirical signature:** a 20-second recording with three SAVE_STATE calls at 5s/10s/15s contains exactly 299 frame records on disk (≈4.98s @ 60fps, the first save mark) — but the file's header claims 1201 frames because RA writes `handle->frame_counter` to the header at end-of-recording. Playback honors the actual disk content, hits clean EOF after the 299 frames, and logs `[Replay] EOF after buttons`. No error is logged during recording.

**The mechanism is narrowed but not fully pinned.** Several plausible explanations remain (see "Open questions" below); confirming requires diagnostic logging in a custom RA build. The diagnostic phase from the original plan is still the right next step, but it should target a different function and include different log statements. See "Plan amendments" below.

**Provisional impact:** the fix is likely ~5-10 lines in `replay_get_serialized_data` (e.g., explicitly seeking back to `file_end` after the rewind+read, or wrapping the read in something that doesn't disturb the writable file's state). Smaller scope than the original plan implied.

---

## What was actually tested

### Test setup
- Stock `C:\RetroArch-Win64\retroarch.exe`, version 1.22.2 (confirmed via NCI `VERSION`).
- ROM: Toothpaste (SMW romhack). Game state `PLAYING` at NCI probe time.
- `retroarch.cfg` per SpinLab's required settings; in particular `replay_checkpoint_interval = "0"` (no auto-checkpoints), `replay_max_keep = "99"`, `cheevos_hardcore_mode_enable = "false"`, `run_ahead_secondary_instance = "true"`, `savestate_file_compression = "true"` (which produces rzip-wrapped `.state` files).

### Probes run
1. `python scripts/probe_bsv_record_with_saves.py --record-no-saves` — 20s recording, no saves. (Control.)
2. `python scripts/probe_bsv_record_with_saves.py --record-with-saves` — 20s recording, SAVE_STATE fired at 5s/10s/15s.
3. `python scripts/probe_bsv_record_with_saves.py --play` — `PLAY_REPLAY` against whatever runtime slot RA was at (replay66, the with-saves recording).

User played the game during the 20s window in each recording mode.

### Empirical results

**File sizes:**
| File | Bytes | Header `frame_count` | Actual frame records on disk |
|---|---|---|---|
| `Toothpaste.replay65` (no saves) | 126,920 | 1201 | **1201** ✓ |
| `Toothpaste.replay66` (3 saves) | 105,640 | 1201 | **299** ✗ |

299 frames ÷ 60 fps = 4.98 seconds — the exact moment of the first SAVE_STATE in the probe.

The 21KB file-size delta is small because the bulk of the file (~98KB) is the initial-state STATESTREAM-encoded checkpoint written at recording start. The frame records themselves are small (~6-25 bytes each). Earlier in the session I incorrectly read the small delta as evidence the bug had been fixed — actually the file-size signal is dominated by the initial checkpoint and is uninformative about input-track length. **The frame-record parser (Appendix A) is the load-bearing measurement, not file size.**

**RA log lines (relevant excerpts from `C:\RetroArch-Win64\logs\retroarch__*.log`):**
```
[INFO] [Replay] Found last replay slot: #64
[INFO] [Replay] Starting movie record to ".../Toothpaste.replay65".
[INFO] [Replay] Stopping movie record.
[INFO] [Replay] Starting movie record to ".../Toothpaste.replay66".
[INFO] [State] Saving state ".../Toothpaste.state532", 929088 bytes.
[INFO] [State] Saving state ".../Toothpaste.state533", 929088 bytes.
[INFO] [State] Saving state ".../Toothpaste.state534", 929088 bytes.
[INFO] [Replay] Stopping movie record.
[INFO] [Replay] Starting movie playback.
[INFO] [Replay] EOF after buttons
[INFO] [Replay] Input replay movie playback ended.
```

Notable absences: **no `[ERROR] [Replay] failed to write checkpoint, exiting record` line**, and **no other error during the recording**. The original plan's Phase 2 expected that error log to appear and to identify which `goto exit` site fired in `bsv_movie_write_checkpoint`. It does not appear in this configuration because `bsv_movie_write_checkpoint` is **not called at all** in our test (interval=0 means no auto-checkpoints, and the SAVE_STATE path does not set `BSV_FLAG_MOVIE_FORCE_CHECKPOINT` — see source-code findings below).

**`[Replay] EOF after buttons`** is logged from [`bsvmovie.c:840`](file:///c:/Users/thedo/git/RetroArch/input/bsv/bsvmovie.c#L840) during playback when `intfstream_read` of `key_event_count` returns 0 bytes — i.e., a clean EOF between frames. The comment in the source calls this `/* Natural(?) EOF */`, with the question mark betraying that the RA authors are themselves unsure when this is "natural" vs "premature."

---

## Source-code findings (where the original plan was wrong)

### Original plan's hypothesis: `bsv_movie_write_checkpoint`

The plan, drawing on `slot-management.md`, identified six `goto exit` sites in `bsv_movie_write_checkpoint` and proposed adding `RARCH_ERR` at each to pin which step returns -1 on a force-checkpoint write. Phase 3 then proposed a RAW-encoding fallback for force-checkpoints.

**This entire framing is wrong for the current bug.** Reasoning:

1. **`SAVE_STATE` does not set `BSV_FLAG_MOVIE_FORCE_CHECKPOINT`.** Searching the entire RA source, only one place sets this flag: [`movie_commit_checkpoint` at `bsvmovie.c:1850`](file:///c:/Users/thedo/git/RetroArch/input/bsv/bsvmovie.c#L1850), which is invoked **only** by `CMD_EVENT_SAVE_REPLAY_CHECKPOINT` (a separate NCI command, [`command.h:495`](file:///c:/Users/thedo/git/RetroArch/command.h#L495)). The `CMD_EVENT_SAVE_STATE` handler at [`command.c:2235`](file:///c:/Users/thedo/git/RetroArch/command.c#L2235) calls `content_save_state` and never touches BSV flags or `movie_commit_checkpoint`.

2. **The TODO at `command.c:2238` confirms it:** `/* TODO: Saving state during recording should associate the state with the replay. */`. This is the unimplemented-on-the-load-side half of PR #15070. The save-side serialization (writing the BSV state into the `.state` file) is implemented; the load-side restore-from-replay during state load is not.

3. **`replay_checkpoint_interval = "0"` means no auto-checkpoints fire.** Combined with (1), this means `bsv_movie_write_checkpoint` is never called during the SAVE_STATE flow in our test. The function and its six -1 paths are not in play.

The original plan's diagnostic phase, run as written, would have produced **zero log output** because the instrumented function is never executed.

### What's actually happening: `replay_get_serialized_data`

`CMD_EVENT_SAVE_STATE` flow:

1. [`retroarch.c:3530`](file:///c:/Users/thedo/git/RetroArch/retroarch.c#L3530) — handler bumps `state_slot` if `savestate_auto_index`, then calls `command_event_main_state(CMD_EVENT_SAVE_STATE)`.
2. [`command.c:2249-2250`](file:///c:/Users/thedo/git/RetroArch/command.c#L2249) — calls `content_save_state(state_path, true)`.
3. [`task_save.c:1419`](file:///c:/Users/thedo/git/RetroArch/tasks/task_save.c#L1419) — calls `content_get_serialized_data(&_len)` to allocate and serialize the state buffer **synchronously, in the runloop**.
4. `content_get_serialized_data` calls `content_write_serialized_state` which, at [`task_save.c:404-422`](file:///c:/Users/thedo/git/RetroArch/tasks/task_save.c#L404-L422), checks if `BSV_FLAG_MOVIE_RECORDING | BSV_FLAG_MOVIE_PLAYBACK` is set. **If so, it calls `replay_get_serialized_data(output + 8)`** to write a `RASTATE_REPLAY_BLOCK` into the state buffer.
5. After `content_get_serialized_data` returns, the SAVE_STATE handler queues `task_push_load_and_save_state` (a `TASK_TYPE_BLOCKING` task) to write the buffer to disk.

The synchronous step (4) is where the BSV file gets touched. Verbatim source ([`bsvmovie.c:1121-1147`](file:///c:/Users/thedo/git/RetroArch/input/bsv/bsvmovie.c#L1121-L1147)):

```c
bool replay_get_serialized_data(void* buffer)
{
   input_driver_state_t *input_st = input_state_get_ptr();
   bsv_movie_t *handle            = input_st->bsv_movie_state_handle;

   if (input_st->bsv_movie_state.flags & (BSV_FLAG_MOVIE_RECORDING | BSV_FLAG_MOVIE_PLAYBACK))
   {
      int32_t file_end        = (uint32_t)intfstream_tell(handle->file);
      int64_t read_amt        = 0;
      int32_t file_end_       = swap_if_big32(file_end);
      uint8_t *buf;
      ((uint32_t *)buffer)[0] = file_end_;
      buf                     = ((uint8_t *)buffer) + sizeof(uint32_t);
      intfstream_rewind(handle->file);
      read_amt                = intfstream_read(handle->file, buf, file_end);
      if (handle->frame_counter > UINT32_MAX) {
         RARCH_ERR("[Replay] Frame counter too big to fit in 32 bits\n");
         return false;
      }
      ((uint32_t *)buffer)[1+REPLAY_HEADER_FRAME_COUNT_INDEX] = swap_if_big32((uint32_t)(handle->frame_counter));
      if (read_amt != file_end)
         RARCH_ERR("[Replay] Failed to write correct number of replay bytes into state file: %d / %d.\n",
               read_amt, file_end);
   }
   return true;
}
```

After this function:
- `intfstream_rewind(handle->file)` resets the cursor to 0.
- `intfstream_read(handle->file, buf, file_end)` reads `file_end` bytes — under normal stdio semantics, the cursor ends up at offset `file_end`, which equals the file's current length.
- **No explicit seek to restore the cursor.** Relies on read-advances-cursor semantics.

Then control returns to the runloop. On the next frame, `bsv_movie_next_frame` is called and enters the write branch ([`bsvmovie.c:991`](file:///c:/Users/thedo/git/RetroArch/input/bsv/bsvmovie.c#L991)). It increments `handle->frame_counter`, computes a backref using `frame_pos[counter-2]` and the current `intfstream_tell` value, writes the backref + key/input events + frame token, and truncates the file to the new cursor position. **Empirically, after the first SAVE_STATE, the file does not actually grow** — but `frame_counter` and `frame_pos` keep advancing as if writes were succeeding.

The `RARCH_ERR("Failed to write correct number of replay bytes...")` at line 1142 does not fire (it would have appeared in our log). So `read_amt == file_end` — the read succeeded fully.

The mechanism by which subsequent `intfstream_write` calls land nowhere is the open question.

---

## Mechanism hypotheses (resolved — see Resolution section at top)

At investigation-time I could not pin the exact mechanism from source reading alone. Candidates, ordered by my prior at the time:

1. **`intfstream_rewind` puts the file in an odd state for a `WRITE | READ` stream.** RA's `intfstream` abstraction may handle rewind in a way that the underlying file's writable-position tracking gets confused. **Confirmed correct in spirit.** The actual answer is one level deeper: not intfstream-specific, but the underlying C stdio update-mode rule (`r+` requires a positioning call between input and output). intfstream wraps stdio and inherits the constraint.

2. **The intfstream_read past EOF may have an intfstream-internal effect** that subsequent writes don't recover from cleanly. Ruled out — no EOF-past behavior involved; the read of `file_end` bytes terminates exactly at EOF, no overshoot.

3. **A flag I didn't find sets `handle->playback = true` or `BSV_FLAG_MOVIE_SEEKING` indirectly.** Ruled out — neither flag flips during SAVE_STATE.

4. **The TASK_TYPE_BLOCKING save task is interfering.** Ruled out as the proximate cause. The blocking task runs after `replay_get_serialized_data` has already done the read; it's the post-read state of the BSV file handle that's the actual problem, not the blocking pause.

Hypothesis (1) was correct at the high level. The diagnostic phase from the plan would have surfaced "writes are being attempted, cursor tracker shows file growing, but on-disk bytes don't change" — which is the C update-mode signature.

---

## Plan amendments (for the agent integrating this)

### Phase 1 (reproduce on freshly-built stock binary)
**No change**, except: don't rely on the absence of file-size truncation as evidence the bug is gone. Use the parser script (Appendix A) to count actual frame records vs the header's claim. Original plan's "the file is shorter, so it's truncated" mental model is wrong; the real signal is `parsed_frames < header_frame_count`.

### Phase 2 (diagnostic logging) — INSTRUMENT A DIFFERENT FUNCTION
Original plan instrumented `bsv_movie_write_checkpoint`. That function is not called in this bug's flow. **Replace Phase 2 with the following.**

Add `RARCH_LOG` instrumentation at three sites:

**Site A: `replay_get_serialized_data` ([`bsvmovie.c:1121`](file:///c:/Users/thedo/git/RetroArch/input/bsv/bsvmovie.c#L1121))** — log entry, the file_end value before rewind, and the cursor position after the read:

```c
bool replay_get_serialized_data(void* buffer)
{
   input_driver_state_t *input_st = input_state_get_ptr();
   bsv_movie_t *handle            = input_st->bsv_movie_state_handle;

   if (input_st->bsv_movie_state.flags & (BSV_FLAG_MOVIE_RECORDING | BSV_FLAG_MOVIE_PLAYBACK))
   {
      int32_t file_end = (uint32_t)intfstream_tell(handle->file);
      RARCH_LOG("[ReplayDiag] get_serialized: BEFORE rewind, file_end=%d, cursor=%d, recording=%d, playback=%d\n",
            file_end,
            (int)intfstream_tell(handle->file),
            !!(input_st->bsv_movie_state.flags & BSV_FLAG_MOVIE_RECORDING),
            !!(input_st->bsv_movie_state.flags & BSV_FLAG_MOVIE_PLAYBACK));
      int64_t read_amt = 0;
      int32_t file_end_ = swap_if_big32(file_end);
      uint8_t *buf;
      ((uint32_t *)buffer)[0] = file_end_;
      buf = ((uint8_t *)buffer) + sizeof(uint32_t);
      intfstream_rewind(handle->file);
      RARCH_LOG("[ReplayDiag] get_serialized: AFTER rewind, cursor=%d\n", (int)intfstream_tell(handle->file));
      read_amt = intfstream_read(handle->file, buf, file_end);
      RARCH_LOG("[ReplayDiag] get_serialized: AFTER read, cursor=%d, read_amt=%lld\n",
            (int)intfstream_tell(handle->file), (long long)read_amt);
      /* ... rest of original function ... */
```

**Site B: `bsv_movie_next_frame` write-branch entry ([`bsvmovie.c:991-1051`](file:///c:/Users/thedo/git/RetroArch/input/bsv/bsvmovie.c#L991-L1051))** — log the frame_counter, cursor, and which branch taken:

```c
if (!handle->playback && !(input_st->bsv_movie_state.flags & BSV_FLAG_MOVIE_SEEKING))
{
   RARCH_LOG("[ReplayDiag] next_frame WRITE: counter=%llu cursor=%d playback=%d seeking=%d\n",
         (unsigned long long)handle->frame_counter,
         (int)intfstream_tell(handle->file),
         (int)handle->playback,
         !!(input_st->bsv_movie_state.flags & BSV_FLAG_MOVIE_SEEKING));
   /* ... existing write logic ... */
}
else
{
   RARCH_LOG("[ReplayDiag] next_frame READ-BRANCH: counter=%llu cursor=%d playback=%d seeking=%d\n",
         (unsigned long long)handle->frame_counter,
         (int)intfstream_tell(handle->file),
         (int)handle->playback,
         !!(input_st->bsv_movie_state.flags & BSV_FLAG_MOVIE_SEEKING));
   /* ... existing read logic ... */
}
```

**Site C: post-write file size, every Nth frame** — to confirm whether writes are landing or no-op'ing:

```c
/* immediately after intfstream_truncate at the end of the write block: */
if (handle->frame_counter % 30 == 0)
{
   intfstream_t *f = handle->file;
   /* a way to ask "what is current file size" — this is intfstream-dependent;
      for fopen-based intfstreams: intfstream_get_size or seek-end + tell */
   int64_t cur_pos = intfstream_tell(f);
   RARCH_LOG("[ReplayDiag] post-write at frame %llu: cursor=%lld\n",
         (unsigned long long)handle->frame_counter, (long long)cur_pos);
}
```

**Run the probe with `--record-with-saves`. Read the log.** The expected diagnostic outcomes:

- If `[ReplayDiag] next_frame WRITE` lines stop appearing after the first SAVE_STATE → writes aren't being attempted; `next_frame` is going into the read branch. Look for what set `playback` or `SEEKING`.
- If `WRITE` lines continue but `cursor` post-write stops advancing → writes are being attempted but `intfstream_write` is silently no-op'ing. The intfstream layer is the suspect.
- If `cursor` continues advancing in the log but the file size on disk doesn't grow → the cursor tracker has decoupled from physical writes; intfstream-level corruption.
- If `get_serialized: AFTER read, cursor` is not equal to `file_end` → cursor isn't where we expect; explicit seek to file_end is the fix. (This is the simplest possible outcome.)

### Phase 3 (the fix) — CONFIRMED
Original plan proposed a RAW-encoding fallback in `bsv_movie_next_frame`. **That fix is not relevant** to the actual bug.

The actual fix is **one line** in `input/bsv/bsvmovie.c`, immediately after the existing `intfstream_read(handle->file, buf, file_end)` in `replay_get_serialized_data`:

```c
intfstream_seek(handle->file, file_end, SEEK_SET);
```

This is the explicit "positioning call between input and output" required by C99/C11 §7.21.5.3 for streams opened in update mode. After the read leaves the stream in input mode, this seek transitions it back to a state where the next `intfstream_write` from `bsv_movie_next_frame` is well-defined.

**Why not just remove the `intfstream_rewind`?** The rewind is necessary for the function's purpose — it needs to read the entire BSV file from offset 0 to embed in the `.state` file's `RASTATE_REPLAY_BLOCK`. The fix is the trailing seek, not removing the rewind.

**Why `SEEK_SET` to `file_end` and not `SEEK_END` to 0?** The file at this point has length exactly `file_end` (the recording is being truncated to current cursor every frame), so `SEEK_SET, file_end` and `SEEK_END, 0` are equivalent. `SEEK_SET, file_end` is more explicit about intent.

### Phase 4 (verification)
**No change.** Three regression tests still apply: no-saves recording works, with-saves recording works, long mixed recording works. Add: parser-based check that `parsed_frames == header_frame_count` for all three.

### Phase 5 (PR upstream)
**Narrative changes:**
- Reference PR #15070 ("Associate states with replays") — that's the PR that introduced `replay_get_serialized_data`.
- Note that the on-save serialization is implemented but interferes with subsequent recording-side writes.
- This is a clean, well-scoped fix. Not the broader RAW-fallback architectural change the original plan implied.

---

## Implications for SpinLab docs

### `slot-management.md`
The "SAVE_STATE during BSV recording is broken in current RA — confirmed hard constraint" section is **correct in spirit but wrong in mechanism**. Specifically:

- The `replay_checkpoint_interval` test matrix is misleading: the checkpoint interval has nothing to do with the failure mode actually observed (no checkpoints fire when interval=0 or under SAVE_STATE; the checkpoint write path is not the trigger).
- The four "workarounds we considered" are all still valid options. Workaround 4 (RA patch) is the recommended path and now has a focused scope.
- The `bsv_movie_write_checkpoint` deep-dive should be retracted or annotated as "this turned out to be the wrong function — see upstream-fix-findings-2026-05-09.md."

### `path-to-parity.md` / `project_post_migration_cleanup.md` memory
The "BSV+SAVE_STATE incompatibility" line is still accurate as a deferred follow-up. Update it to reference this report and the upstream-fix-plan.

### `project_retroarch_migration_status.md` memory
Status section "Phase E option (b) — replay → segment capture" is still deferred. The blocker is now better understood. The other two Phase E blockers (slot resolution, poller starvation) are unchanged and unrelated.

---

## Open questions for the agent

1. **What is the actual silent-failure mechanism?** Phase 2 diagnostic will answer this.
2. **Does the same bug affect SAVE_STATE during PLAYBACK?** The same `replay_get_serialized_data` runs for both `MOVIE_RECORDING` and `MOVIE_PLAYBACK` flags. Likely yes; worth a single test once Phase 1 reproduces the recording-side bug.
3. **Is the fix at the bsvmovie.c level (cursor-restoration) or the intfstream level (rewind semantics)?** Depends on Phase 2 outcome.
4. **Has anyone reported this upstream?** A search of libretro/RetroArch issues for "save state replay record" / "checkpoint truncated" turned up only [#15806](https://github.com/libretro/RetroArch/issues/15806) (a confused user-side bug report on RA 1.16.0, unrelated). This bug appears to be unreported upstream. Consider filing an issue with the probe + parser + this report linked, separately from any fix PR.

---

## What was deliberately not done

- **`SAVE_STATE_TO_RAM` test.** Not exposed via NCI; would require cfg + manual hotkey. Skipped.
- **Splice two .replay files.** Format reading already showed splicing requires parsing the BSV frame stream and rewriting backrefs + dedup indices. Not feasible without significant tooling, and not necessary for any practical use case.
- **`intfstream_rewind`/`intfstream_read` source review.** The intfstream impl lives in libretro-common (not the RetroArch repo). For the diagnostic phase, the inline cursor logging in Site A is sufficient; only revisit if Phase 2 shows the cursor is in an unexpected state.
- **Decompressing the rzip-wrapped `.state` files** to verify the `RASTATE_REPLAY_BLOCK` content. The bug's signature is in the `.replay` file, not the `.state` file; the `.state` file content is informationally redundant.

---

## Artifacts produced this session

- **`scripts/count_replay_frames.py`** — BSV v2 parser that reads the header and counts actual frame records on disk. Used to definitively establish that `Toothpaste.replay66` has 299 frame records despite a header claim of 1201. Reusable as a fixture-validation tool for any future BSV work.
- **This report.**

---

## Appendix A — `count_replay_frames.py` invocation reference

```bash
$ python scripts/count_replay_frames.py path/to/file.replay [more.replay ...]
```

Output (from this session):
```
file: Toothpaste.replay65  total: 126920 bytes
  magic=0x42535632 vsn=2 crc=0x41B3C49D state_size=98056
  frame_count_header=1201 block_size=128 sb_size=16
  ckpt_cfg=0x04020200  commit_interval=4 commit_threshold=2 compression=2
  initial ckpt: compression=2 encoding=1
  initial ckpt: unc_size=823407 enc_size=225166 comp_size=98042
  parsed 1201 frame records (0 checkpoint frames, rest regular)
  header says frame_count=1201, parsed=1201, MATCH

file: Toothpaste.replay66  total: 105640 bytes
  magic=0x42535632 vsn=2 crc=0x41B3C49D state_size=98424
  frame_count_header=1201 block_size=128 sb_size=16
  ckpt_cfg=0x04020200  commit_interval=4 commit_threshold=2 compression=2
  initial ckpt: compression=2 encoding=1
  initial ckpt: unc_size=823407 enc_size=224500 comp_size=98410
  parsed 299 frame records (0 checkpoint frames, rest regular)
  header says frame_count=1201, parsed=299, MISMATCH (truncated)
```

The MATCH/MISMATCH line is the load-bearing signal. File-size deltas alone are not sufficient because the initial-state checkpoint dominates the file size.

Token values: `REPLAY_TOKEN_REGULAR_FRAME = 'f' = 102`, `REPLAY_TOKEN_CHECKPOINT2_FRAME = 'C' = 67`. (Defined in `input/input_driver.h:125-128`.)

---

## Appendix B — Source code references

In `input/bsv/bsvmovie.c` at the v1.22.2 tag (commit `69a4f0ea1e`):

| Line | What |
|---|---|
| 968 | `bsv_movie_next_frame` definition |
| 991 | the `if (!playback && !SEEKING)` write-vs-read branch — the gate for whether frame data gets written |
| 994-1051 | the write block (writes backref, key events, input events, frame token, truncate) |
| 1052-1057 | the read-branch fallback (calls `bsv_movie_read_next_events`) |
| 1058 | `frame_pos[counter & mask] = intfstream_tell(file)` — updated regardless of branch |
| **1121** | **`replay_get_serialized_data` — the actual culprit** |
| 1134-1135 | the `intfstream_rewind` + `intfstream_read` pair |
| 1142 | `read_amt != file_end` error log (does not fire in our test) |
| 1850 | the only place `BSV_FLAG_MOVIE_FORCE_CHECKPOINT` is set (only via `SAVE_REPLAY_CHECKPOINT` NCI command) |

In `tasks/task_save.c`:

| Line | What |
|---|---|
| 73 | `RASTATE_REPLAY_BLOCK = "RPLY"` marker |
| 404-422 | the `if RECORDING|PLAYBACK then write replay block` block in `content_write_serialized_state` |
| **418** | **the call to `replay_get_serialized_data` that triggers the bug** |
| 1258 | `task_push_load_and_save_state` (TASK_TYPE_BLOCKING) |
| 1299 | `task->type = TASK_TYPE_BLOCKING` |

In `command.c`:

| Line | What |
|---|---|
| 2235 | `CMD_EVENT_SAVE_STATE` handler entry |
| 2238 | the TODO comment confirming SAVE_STATE↔replay association is not finished on the load side |
| 2249-2250 | the `content_save_state` call |

In `command.h`:

| Line | What |
|---|---|
| 442-467 | the NCI `action_map` (commands that take arguments) |
| 469-510 | the NCI `map` (commands that map to RARCH_*_KEY hotkey codes) |
| 488 | `"SAVE_STATE" → RARCH_SAVE_STATE_KEY` (the NCI command we're firing) |
| 495 | `"SAVE_REPLAY_CHECKPOINT" → RARCH_SAVE_REPLAY_CHECKPOINT_KEY` (the *other* command, which would set FORCE_CHECKPOINT — but we are not using this) |

---

## Appendix C — Diff summary: bsvmovie.c v1.22.2 vs master

`git log v1.22.2..upstream/master --oneline -- input/bsv/bsvmovie.c` shows three relevant commits:

- `dcf6cc7520` Warning fixes
- `003a1ff74b` input/bsv: bound key_event_count and input_event_count against array size
- `1fe435a905` bsv, cdfs: free intfstream_t after intfstream_close at five remaining leak sites

**None of these touch `replay_get_serialized_data` or `bsv_movie_next_frame` write logic.** The bug is unfixed in master. There is no upstream cherry-pick available; we'd need to author the fix.

(One nice-to-have side effect: the `1fe435a9` leak fix should probably be cherry-picked into our patch series — `bsv_movie_write_deduped_state` leaks an `intfstream_t` per checkpoint in v1.22.2, fixed in master with one `free(out_stream)` call. Small, unrelated, but easy to bundle with a real fix PR.)
