# RetroArch BSV Force-Checkpoint Bug — Diagnostic + Fix Plan

**For:** a Claude Code agent working in a fresh `libretro/RetroArch` fork checkout.
**Source project that found the bug:** SpinLab (`c:\Users\thedo\git\spinlab`). You don't need to read SpinLab's code; the relevant test probe is inlined below.
**Target RA version:** `v1.22.2` (release tag, 2025-11-20). PRs land on `master`.

---

## TL;DR

When `SAVE_STATE` fires while `RECORD_REPLAY` is active, RA's `bsv_movie_write_checkpoint()` returns -1, RA flips `BSV_FLAG_MOVIE_END`, and the in-progress `.replay` file is truncated. With `replay_checkpoint_interval = "0"` (default) the failure is silent; with `"1"` it logs `[ERROR] [Replay] failed to write checkpoint, exiting record`. The function has six possible -1 return paths and **none of them log which one fired** — the diagnostic gap is itself the first thing to fix.

The strategy is staged:
1. Build stock RA from source, confirm the bug reproduces on the freshly-built binary.
2. Add `RARCH_ERR` logging at each `goto exit` site in `bsv_movie_write_checkpoint`. Rebuild, run probe, identify the failing step.
3. Fix the failing step. Most likely culprit is the STATESTREAM dedup encoder; the file format already supports per-checkpoint encoding, so a RAW-encoding fallback for force-checkpoints is a ~5-line patch that doesn't change playback compatibility.
4. PR upstream — diagnostic logging as a standalone observability PR, fix as a second PR.

The diagnostic phase has standalone value: it ends years of guessing about which step fails. Even if the fix path turns out different than expected, the logging belongs upstream regardless.

---

## Background

### What's broken

`SAVE_STATE` during `RECORD_REPLAY` corrupts the recording. Confirmed direct to NCI (no SpinLab in the loop) on RA 1.22.2 with both `snes9x_libretro` and `bsnes_libretro` cores.

Test matrix (run with the probe in Appendix A):

| `replay_checkpoint_interval` | Result with 3 SAVE_STATE calls in 20s recording |
|---|---|
| `"0"` (default) | Playback EOFs at first SAVE_STATE point. RA log says clean `Stopping movie record`. Input track truncated. |
| `"1"` (1 sec interval) | RA log shows `[ERROR] [Replay] failed to write checkpoint, exiting record`. Recording silently ends. |
| `"60"` (1 min interval) | Same as `"0"` — interval is in seconds, doesn't fire in 20s window. |

### What we know from source reading

File: [`input/bsv/bsvmovie.c`](https://github.com/libretro/RetroArch/blob/v1.22.2/input/bsv/bsvmovie.c) at the v1.22.2 tag.

The relevant call chain is `bsv_movie_next_frame` → `bsv_movie_write_checkpoint`:

```c
// bsv_movie_next_frame at line ~1016:
if ((input_st->bsv_movie_state.flags & BSV_FLAG_MOVIE_FORCE_CHECKPOINT)
    || ((checkpoint_interval != 0)
        && (handle->frame_counter > 0)
        && (handle->frame_counter % (checkpoint_interval*60) == 0)))
{
    uint8_t frame_tok   = REPLAY_TOKEN_CHECKPOINT2_FRAME;
    uint8_t compression = handle->checkpoint_compression;
#if HAVE_STATESTREAM
    uint8_t encoding    = REPLAY_CHECKPOINT2_ENCODING_STATESTREAM;
#else
    uint8_t encoding    = REPLAY_CHECKPOINT2_ENCODING_RAW;
#endif
    input_st->bsv_movie_state.flags &= ~BSV_FLAG_MOVIE_FORCE_CHECKPOINT;
    intfstream_write(handle->file, (uint8_t *)(&frame_tok), sizeof(uint8_t));
    intfstream_write(handle->file, (uint8_t *)(&compression), sizeof(uint8_t));
    intfstream_write(handle->file, (uint8_t *)(&encoding), sizeof(uint8_t));
    if (bsv_movie_write_checkpoint(handle, compression, encoding) < 0)
    {
        RARCH_ERR("[Replay] failed to write checkpoint, exiting record\n");
        input_st->bsv_movie_state.flags |= BSV_FLAG_MOVIE_END;
    }
}
```

Three crucial observations:

1. **STATESTREAM is hardcoded** when `HAVE_STATESTREAM` is compiled in (which it is in stock builds). There is no cfg knob to force RAW for force-checkpoints.
2. **Force-checkpoint and auto-interval go through the same code path.** Same function call, same failure surface.
3. **The encoding byte is per-checkpoint in the file format.** Each checkpoint writes its own `compression` and `encoding` byte before the payload (`intfstream_write` of `&encoding` above). The playback path (line ~334, `bsv_movie_load_checkpoint`) dispatches on those bytes. A single `.replay` file can mix RAW and STATESTREAM checkpoints with no compatibility break.

The six -1 paths in `bsv_movie_write_checkpoint`:

```c
int64_t bsv_movie_write_checkpoint(bsv_movie_t *handle, uint8_t compression, uint8_t encoding)
{
    // ... allocation + core_serialize ...
    switch (encoding)
    {
        case REPLAY_CHECKPOINT2_ENCODING_RAW:
            // no failure path here
            break;
#ifdef HAVE_STATESTREAM
        case REPLAY_CHECKPOINT2_ENCODING_STATESTREAM:
            encoded_size = (uint32_t)bsv_movie_write_deduped_state(...);
            // [SUSPECT 1] dedup can return short or zero on inconsistent buffer state
            break;
#endif
        default:
            ret = -1; goto exit;     // [SITE 1] unknown encoding
    }
    switch (compression)
    {
        case REPLAY_CHECKPOINT2_COMPRESSION_NONE: break;
#ifdef HAVE_ZLIB
        case REPLAY_CHECKPOINT2_COMPRESSION_ZLIB:
            if (compress2(...) != Z_OK) { ret = -1; goto exit; }   // [SITE 2]
            break;
#endif
#ifdef HAVE_ZSTD
        case REPLAY_CHECKPOINT2_COMPRESSION_ZSTD:
            if (ZSTD_isError(...)) { ret = -1; goto exit; }        // [SITE 3]
            break;
#endif
        default:
            ret = -1; goto exit;     // [SITE 4] unknown compression
    }
    if (intfstream_write(...) < sizeof(uint32_t)) { ret = -1; goto exit; } // [SITE 5a-d] 4 short-write checks
    if (intfstream_write(...) < sizeof(uint32_t)) { ret = -1; goto exit; }
    if (intfstream_write(...) < sizeof(uint32_t)) { ret = -1; goto exit; }
    if (intfstream_write(handle->file, compressed_encoded_data, compressed_encoded_size)
        < compressed_encoded_size) { ret = -1; goto exit; }
    ret = 3 * sizeof(uint32_t) + compressed_encoded_size;
exit:
    // swap cur_save <-> last_save unconditionally
    // ... cleanup ...
    return ret;
}
```

### Working hypothesis

Most likely culprit is the dedup encoder (`bsv_movie_write_deduped_state`) on a force-checkpoint following a previous checkpoint. Reasoning:

- The function unconditionally swaps `handle->cur_save` ↔ `handle->last_save` at the `exit:` label on every call (success or failure). After the first force-checkpoint, `last_save` holds the previous serialized state.
- `bsv_movie_write_deduped_state` (line ~1507) gates on `movie->cur_save_valid && movie->last_save && movie->last_save_size >= state_size` to decide whether incremental dedup is safe.
- If two force-checkpoints fire close together (rapid SAVE_STATE during recording, exactly SpinLab's pattern at 5s/10s/15s), the buffer-swap leaves an inconsistent state that the dedup encoder can't handle.
- The dedup function returns `intfstream_tell(out_stream)`. If the memory stream's writes silently failed (e.g. allocation too small), the returned size is wrong but not negative — but downstream the casts and compression bounds get garbage. Some path through this leads to one of SITES 1-5 returning -1.

This is a hypothesis, not a confirmed root cause. **Phase 2 logging is required to confirm.**

### What's NOT the bug

- It's not a missing feature: PR [#15070 (2023-03)](https://github.com/libretro/RetroArch/pull/15070) "Associate states with replays" shipped well before v1.22.2 (2025-11). The shipped code is supposed to handle this.
- It's not config: the cfg surface for replay checkpoints is `replay_checkpoint_interval` and `replay_checkpoint_deserialize`. Both have been tested. Compression is compile-time, not cfg-driven.
- It's not the core: snes9x and bsnes both fail. bsnes also blocks runahead so isn't usable for this user anyway.

---

## Phase 0 — Build setup (prerequisite, ~1 hour)

Skip if RA already builds from source on this machine.

### Install MSYS2 + toolchain

1. Install MSYS2 from https://www.msys2.org/.
2. Open the MSYS2 MinGW64 shell. Run:
   ```bash
   pacman -Syu                  # update package db, may require restart
   pacman -S --needed wget git make \
       mingw-w64-x86_64-toolchain \
       mingw-w64-x86_64-ntldd \
       mingw-w64-x86_64-zlib \
       mingw-w64-x86_64-pkg-config \
       mingw-w64-x86_64-SDL2 \
       mingw-w64-x86_64-libxml2 \
       mingw-w64-x86_64-freetype \
       mingw-w64-x86_64-python3 \
       mingw-w64-x86_64-ffmpeg \
       mingw-w64-x86_64-drmingw
   ```
3. Source: [Libretro Windows MSYS2 build docs](https://docs.libretro.com/development/retroarch/compilation/windows/).

### Clone the fork

The user has forked `libretro/RetroArch` to their account. Clone:

```bash
cd c:/Users/thedo/git
git clone https://github.com/<USERNAME>/RetroArch.git
cd RetroArch
git remote add upstream https://github.com/libretro/RetroArch.git
git fetch upstream --tags
git checkout v1.22.2
```

### First build

```bash
./configure
make clean
make -j4
```

`retroarch.exe` lands in the source root. ~30 minutes on first build, faster on incrementals.

### Confirm it runs

```bash
./retroarch.exe --version
```

Should print version info matching v1.22.2.

**Exit criterion:** `retroarch.exe` runs and prints the version string.

---

## Phase 1 — Reproduce the bug on the fresh build (~15 min)

The probe script is in Appendix A. It uses RA's NCI (UDP 55355) to drive recording without any SpinLab dependencies.

### Setup

Find the user's `retroarch.cfg` (typically `C:\RetroArch-Win64\retroarch.cfg` or `%APPDATA%/retroarch/retroarch.cfg`). Required settings:

```
network_cmd_enable = "true"
network_cmd_port = "55355"
cheevos_hardcore_mode_enable = "false"
run_ahead_secondary_instance = "true"
replay_max_keep = "99"
log_to_file = "true"
log_to_file_timestamp = "true"
log_verbosity = "true"
replay_checkpoint_interval = "0"
```

The non-obvious ones: `cheevos_hardcore_mode_enable = "false"` (RA silently drops NCI savestate commands when on), `run_ahead_secondary_instance = "true"` (single-instance runahead corrupts state buffers), `replay_max_keep = "99"` (default `"0"` silently blocks new recordings when files exist).

### Run the probe

1. Launch the freshly-built `retroarch.exe` with a SNES ROM (any ROM works — SMW or its hacks are typical).
2. Get into a level / playable context.
3. From a separate shell, run the probe (Appendix A):
   ```bash
   python probe_bsv_record_with_saves.py --record-with-saves
   ```
4. While the probe is running, play the game (move Mario, jump). The probe fires SAVE_STATE at the 5s/10s/15s marks.
5. After the probe halts recording, run:
   ```bash
   python probe_bsv_record_with_saves.py --play
   ```
6. Watch RA's window. **Expected (buggy) behavior:** playback EOFs after a few seconds (~5 seconds in, at the first SAVE_STATE point). The full 20s does not play back.

Also check `C:\RetroArch-Win64\logs\retroarch__*.log` (or wherever `log_to_file` writes) for the most recent log file. With `replay_checkpoint_interval = "0"` it should say something like `Stopping movie record` cleanly. With `"1"` it should say `[ERROR] [Replay] failed to write checkpoint, exiting record`. Try both interval values and confirm both modes reproduce.

**Exit criterion:** the bug reproduces on this freshly-built binary. If it doesn't, something is different about your build environment vs. the user's stock binary — investigate before continuing (most likely cause: `HAVE_STATESTREAM` not compiled in for some reason, falling through to RAW which doesn't fail).

---

## Phase 2 — Diagnostic logging (~30 min)

Add `RARCH_ERR` calls at each `goto exit` site in `bsv_movie_write_checkpoint`. Rebuild, run the probe again, read the log to identify which site fires.

### The patch

Edit `input/bsv/bsvmovie.c`. In `bsv_movie_write_checkpoint` (around line 669), add `RARCH_ERR` immediately before each `goto exit` that sets `ret = -1`. Concretely (line numbers approximate):

```c
// Site 1: unknown encoding (default in encoding switch)
default:
    RARCH_ERR("[Replay] checkpoint -1 SITE 1: unknown encoding %d\n", encoding);
    ret = -1;
    goto exit;
```

```c
// Site 2: zlib compress2 failure
if (compress2(compressed_encoded_data, &zlib_compressed_encoded_size,
              encoded_data, encoded_size, 6) != Z_OK)
{
    RARCH_ERR("[Replay] checkpoint -1 SITE 2: zlib compress2 failed (encoded_size=%u)\n",
              encoded_size);
    ret = -1;
    goto exit;
}
```

```c
// Site 3: ZSTD error
if (ZSTD_isError(compressed_encoded_size_zstd))
{
    RARCH_ERR("[Replay] checkpoint -1 SITE 3: ZSTD error: %s (encoded_size=%u)\n",
              ZSTD_getErrorName(compressed_encoded_size_zstd), encoded_size);
    ret = -1;
    goto exit;
}
```

```c
// Site 4: unknown compression
default:
    RARCH_ERR("[Replay] checkpoint -1 SITE 4: unknown compression %d\n", compression);
    ret = -1;
    goto exit;
```

```c
// Site 5a-d: short writes (4 of them, one per intfstream_write check)
size_ = swap_if_big32((uint32_t)serial_info.size);
if (intfstream_write(handle->file, &size_, sizeof(uint32_t)) < (int64_t)sizeof(uint32_t))
{
    RARCH_ERR("[Replay] checkpoint -1 SITE 5a: short write of serial_info.size\n");
    ret = -1;
    goto exit;
}
size_ = swap_if_big32(encoded_size);
if (intfstream_write(handle->file, &size_, sizeof(uint32_t)) < (int64_t)sizeof(uint32_t))
{
    RARCH_ERR("[Replay] checkpoint -1 SITE 5b: short write of encoded_size (was %u)\n",
              encoded_size);
    ret = -1;
    goto exit;
}
size_ = swap_if_big32(compressed_encoded_size);
if (intfstream_write(handle->file, &size_, sizeof(uint32_t)) < (int64_t)sizeof(uint32_t))
{
    RARCH_ERR("[Replay] checkpoint -1 SITE 5c: short write of compressed_encoded_size (was %u)\n",
              compressed_encoded_size);
    ret = -1;
    goto exit;
}
if (intfstream_write(handle->file, compressed_encoded_data, compressed_encoded_size) < compressed_encoded_size)
{
    RARCH_ERR("[Replay] checkpoint -1 SITE 5d: short write of payload (size=%u)\n",
              compressed_encoded_size);
    ret = -1;
    goto exit;
}
```

Also add a one-shot diagnostic at the top of `bsv_movie_write_checkpoint` to trace input shape:

```c
RARCH_LOG("[Replay] write_checkpoint enter: compression=%u encoding=%u serial_size=%zu cur_save=%p last_save=%p cur_save_valid=%d\n",
          compression, encoding, core_serialize_size(),
          (void*)handle->cur_save, (void*)handle->last_save, handle->cur_save_valid);
```

This tells us, on each call, whether the dedup encoder's input precondition (`cur_save_valid && last_save != NULL`) is met.

### Rebuild and run

```bash
make -j4         # incremental, fast (just bsvmovie.c)
```

Re-run Phase 1's probe. Inspect the RA log file. **Expected output:** one or more `[Replay] checkpoint -1 SITE N` lines pinpointing the failure site, plus `[Replay] write_checkpoint enter` lines with the input shape preceding the failure.

### Exit criterion

You can answer the question: "When SpinLab's reference flow fires SAVE_STATE during RECORD_REPLAY on RA v1.22.2 with snes9x_libretro, which exact step in `bsv_movie_write_checkpoint` returns -1, and what does the input state look like at that point?"

---

## Phase 3 — Fix, based on Phase 2 outcome

### Decision tree

**If Site 5a-d (short write):** the file handle has gone bad. Investigate `intfstream_t` state. Possibly RA closed the underlying file in response to some other state. This is the worst outcome — fix is non-obvious. Open an issue upstream, gather more diagnostics. STOP this plan and discuss with the user.

**If Site 4 (unknown compression):** `handle->checkpoint_compression` got corrupted. Trace where it's set. Probably a struct-init bug in record initialization. Fix is wherever the recording handle is created (search for callers of `bsv_movie_init` or whatever creates the record handle — likely in `runloop.c` or `command.c`).

**If Site 3 (ZSTD error):** zstd compression is failing on a particular state size or encoded shape. Capture the failing input bytes (write `encoded_data` to a debug file in the error branch), reproduce in isolation against zstd's API. Likely a zstd bug or input-precondition bug. Fix could be: catch the error and fall back to NONE compression for this checkpoint.

**If Site 2 (zlib failure):** same as Site 3 but for zlib. Fall back to NONE.

**If Site 1 (unknown encoding):** the encoding parameter is malformed at the call site. Check `bsv_movie_next_frame` — is `HAVE_STATESTREAM` actually defined at compile time? If it is, encoding should be a valid value. Investigate the build flags.

**If "Site 0" — the function returns 0 or returns a wrong value but no -1 path fires:** then `bsv_movie_write_checkpoint` itself isn't the failure point. The truncation is happening elsewhere — possibly in the caller's interpretation of the return value, or in subsequent frames. Re-examine `bsv_movie_next_frame` flow.

**Most likely outcome (working hypothesis):** The diagnostic shows that on the second or third force-checkpoint, `cur_save_valid=0` or `last_save=NULL` or some buffer-state inconsistency, and `bsv_movie_write_deduped_state` returns a value that, after the cast to `uint32_t`, makes downstream writes go bad. In that case:

### The simple fix: RAW fallback for force-checkpoints

If the dedup encoder is unreliable for force-checkpoints, sidestep it. The file format already supports per-checkpoint encoding selection. In `bsv_movie_next_frame`:

```c
#if HAVE_STATESTREAM
    // Force-checkpoints (user-fired SAVE_STATE during record) use RAW encoding to
    // avoid a known issue where the STATESTREAM dedup encoder becomes inconsistent
    // when force-checkpoints fire close together. Auto-interval checkpoints continue
    // to use STATESTREAM for size efficiency.
    bool is_force = (input_st->bsv_movie_state.flags & BSV_FLAG_MOVIE_FORCE_CHECKPOINT) != 0;
    uint8_t encoding = is_force ? REPLAY_CHECKPOINT2_ENCODING_RAW
                                : REPLAY_CHECKPOINT2_ENCODING_STATESTREAM;
#else
    uint8_t encoding = REPLAY_CHECKPOINT2_ENCODING_RAW;
#endif
```

Validate by:
1. Re-running the probe with the patched binary — playback should complete the full 20s with all input replayed.
2. Long no-save recording (Appendix A's `--record-no-saves` mode) still works (regression check on non-force-checkpoint path).
3. Mixed: record long with rare saves, verify playback honors all inputs through and past each save.

### Alternative fix: defensive fallback inside the encoder

If the user (or upstream maintainers) prefer to keep STATESTREAM for force-checkpoints, the fix is in `bsv_movie_write_checkpoint` itself: detect the dedup-encoder failure mode and re-encode as RAW within the same call. More invasive, larger patch, but preserves the size benefit. Worth proposing as an alternative in the upstream PR.

---

## Phase 4 — Verification

The diagnostic build must pass three tests:

1. **No-save recording still works** (regression). Probe with `--record-no-saves`, then `--play`. Full 20s plays back.
2. **Save-during-recording works** (the fix). Probe with `--record-with-saves`, then `--play`. Full 20s plays back; inputs continue past each SAVE_STATE point.
3. **Long mixed recording.** Manually record a 2-3 minute session with 5+ saves at varying intervals. Play back. All inputs honored.

Also: read RA's log file. There should be no `[Replay] checkpoint -1 SITE *` errors during the success cases, and no `failed to write checkpoint, exiting record` line.

If all three pass, the patch is good for upstream submission.

---

## Phase 5 — Upstream PR

### Branching

Switch off v1.22.2 to a feature branch off master:

```bash
git checkout master
git pull upstream master
git checkout -b fix/bsv-force-checkpoint-fallback
```

Re-apply the diagnostic logging + the fix on this branch (manually or via cherry-pick if you committed cleanly on v1.22.2). Verify the fix still works on master — there may be diffs in `bsvmovie.c` between v1.22.2 and master.

### Two PRs, not one

**PR 1: diagnostic logging.** Pure observability — adds `RARCH_ERR` at the existing -1 sites in `bsv_movie_write_checkpoint`, no behavior change. This is a net-positive standalone change and should be easy to land. Small, uncontroversial.

**PR 2: the fix.** Either RAW fallback for force-checkpoints (simpler) or in-encoder fallback (more invasive). Reference PR 1 in the description so reviewers have the diagnostic context. Include in the description:
- The reproducer (link to a gist of the probe, or paste the relevant Python/C snippets)
- Phase 2's diagnostic findings (which site fires, what the input shape was)
- Why the fix is correct (the file format already supports mixed encoding)
- A note that this enables stable replay-while-saving workflows for downstream projects

### Reference points

- BSV recently saw active improvement: PR [#15070](https://github.com/libretro/RetroArch/pull/15070), PR [#17042](https://github.com/libretro/RetroArch/pull/17042). The team takes BSV PRs.
- The relevant maintainer for input/replay is whoever reviewed those — check the PR pages.

### Vendoring while waiting for upstream

If the user wants to use the fix immediately, they swap their installed `retroarch.exe` (typically at `C:\RetroArch-Win64\retroarch.exe`) for the patched build's output. RA cores (`snes9x_libretro.dll`) are unchanged — the patch is frontend-only. Save the original `retroarch.exe` as `retroarch.exe.stock` first, just in case.

---

## Appendix A — The probe script

Save this as `probe_bsv_record_with_saves.py` somewhere on the user's machine. It uses RA's NCI UDP protocol directly — no other dependencies.

```python
"""Probe: BSV/replay recording with mid-record SAVE_STATE calls.

Tests whether SAVE_STATE during a RECORD_REPLAY window corrupts the
resulting .replay file.

Usage:
    1. Have RetroArch already running with a ROM loaded and at a level.
    2. Run this script with one of three modes:
         --record-no-saves    : record 20s with no SAVE_STATE calls
         --record-with-saves  : record 20s with 3 SAVE_STATE calls (default)
         --play               : fire PLAY_REPLAY against whatever RA last wrote
    3. While each --record-* mode runs, play the game.
    4. After --record-* exits, run --play. Watch RA's window:
         - If replay runs full 20s and game does the things you did → BSV intact
         - If replay EOFs after a few seconds → BSV corrupted
"""
from __future__ import annotations

import socket
import sys
import time

NCI_HOST = "127.0.0.1"
NCI_PORT = 55355


def _send(cmd: str) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(cmd.encode("ascii"), (NCI_HOST, NCI_PORT))
    sock.close()


def record_no_saves(duration_s: float = 20.0) -> None:
    print(f"=== RECORD_REPLAY for {duration_s}s, NO SAVE_STATE calls ===\n")
    _send("RECORD_REPLAY")
    for s in range(int(duration_s), 0, -1):
        print(f"  recording... {s}s left", end="\r")
        time.sleep(1.0)
    print()
    print("\n=== HALT_REPLAY ===")
    _send("HALT_REPLAY")
    time.sleep(1.5)
    print("Done. Run with --play to replay.")


def record_with_saves(duration_s: float = 20.0) -> None:
    """Mimics SpinLab's reference flow: RECORD + 3 SAVE_STATE + HALT."""
    print(f"=== RECORD_REPLAY for {duration_s}s, 3 SAVE_STATE calls at 5s/10s/15s ===\n")
    _send("RECORD_REPLAY")
    save_marks = {5, 10, 15}
    for s in range(int(duration_s), 0, -1):
        elapsed = int(duration_s) - s
        if elapsed in save_marks:
            print(f"  recording... {s}s left | firing SAVE_STATE")
            _send("SAVE_STATE")
        else:
            print(f"  recording... {s}s left", end="\r")
        time.sleep(1.0)
    print()
    print("\n=== HALT_REPLAY ===")
    _send("HALT_REPLAY")
    time.sleep(1.5)
    print("Done. Run with --play to replay. Watch for early EOF.")


def play() -> None:
    print("=== PLAY_REPLAY ===")
    print("Watch RA's window for full vs early-EOF playback.")
    _send("PLAY_REPLAY")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--record-with-saves"
    if arg == "--play":
        play()
    elif arg == "--record-no-saves":
        record_no_saves()
    elif arg == "--record-with-saves":
        record_with_saves()
    else:
        print(__doc__)
        sys.exit(1)
```

---

## Appendix B — Key source code references

In `input/bsv/bsvmovie.c` at the `v1.22.2` tag:

| Line | What |
|---|---|
| 669 | `bsv_movie_write_checkpoint` definition |
| 705 | call to `bsv_movie_write_deduped_state` (the suspect) |
| 786-791 | `cur_save` / `last_save` buffer swap at exit (key to the rapid-fire hypothesis) |
| 968 | `bsv_movie_next_frame` definition |
| 1016-1039 | the FORCE_CHECKPOINT vs auto-interval branch |
| 1023-1027 | hardcoded STATESTREAM encoding |
| 1031-1033 | per-checkpoint compression+encoding bytes written to file |
| 1034-1038 | the failure log + flag-set we're trying to avoid |
| 1507 | `bsv_movie_write_deduped_state` definition |
| 1850 | where SAVE_STATE sets `BSV_FLAG_MOVIE_FORCE_CHECKPOINT` |

In `input/bsv/bsvmovie.h`:

- Line 25-42: BSV v2 file format header layout

---

## Appendix C — What this plan deliberately omits

- **Multi-segment recording in SpinLab.** A workaround that doesn't touch RA. The user explicitly chose the upstream-fix route over this because per-segment recordings lock segment boundaries at recording time, which prevents re-deriving captures with new event detectors later.
- **The "splice two .replay files together" experiment.** The file format reading in this plan already shows splicing requires parsing the BSV frame stream and rewriting backref offsets + dedup indices — non-trivial and the practical use case (per-segment capture) doesn't need single-file output.
- **Fixing RA's playback slot resolution** (a separate Phase E follow-up, see SpinLab's `slot-management.md`). Out of scope here; that work is on the SpinLab side.
