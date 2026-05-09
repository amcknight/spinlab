"""Probe: BSV/replay recording with mid-record SAVE_STATE calls.

Tests whether `SAVE_STATE` during a `RECORD_REPLAY` window corrupts the
resulting .replay (the failure mode SpinLab's reference flow keeps hitting).

Usage:
    1. Have RetroArch already running with a ROM loaded and at a level.
       The dashboard does NOT need to be running.
    2. Run this script with one of three modes:
         --record-no-saves    : record 20s with no SAVE_STATE calls
         --record-with-saves  : record 20s with 3 SAVE_STATE calls in the middle
                                (mimics SpinLab's reference flow)
         --play               : fire PLAY_REPLAY against whatever RA last wrote
       Default (no flag): --record-with-saves
    3. While each --record-* mode runs, PLAY THE GAME. Move Mario, jump.
    4. After --record-* exits, run --play. Watch RA's window:
         - If replay runs full 20s and Mario does the things you did → BSV intact
         - If replay EOFs after a few seconds → BSV corrupted by something

Comparison table to fill in:

    | record mode         | playback duration | RA log says           |
    | ------------------- | ----------------- | --------------------- |
    | --record-no-saves   | ?                 | ?                     |
    | --record-with-saves | ?                 | ?                     |

If `--record-no-saves` plays full but `--record-with-saves` EOFs early,
SAVE_STATE during BSV recording is a hard incompatibility. Then try
adjusting `replay_checkpoint_interval` in retroarch.cfg (currently "0";
try "60") and re-run --record-with-saves. If non-zero checkpoint_interval
makes it work, that's our fix. If not, the incompatibility is fundamental.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Add python/ to path for in-place run.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from spinlab.retroarch.nci import NCIClient


def record_no_saves(duration_s: float = 20.0) -> None:
    client = NCIClient()
    print(f"NCI version: {client.version()}")
    print(f"Status: {client.get_status()}\n")

    print(f"=== Firing RECORD_REPLAY. Play the game for {duration_s} seconds. ===")
    print("=== NO SAVE_STATE calls during this run. ===\n")
    client.record_replay()
    for s in range(int(duration_s), 0, -1):
        print(f"  recording... {s} seconds left", end="\r")
        time.sleep(1.0)
    print()
    print("\n=== Firing HALT_REPLAY ===")
    client.halt_replay()
    time.sleep(1.5)
    print("Done. Run with --play to replay.")


def record_with_saves(duration_s: float = 20.0) -> None:
    """Mimics SpinLab's reference flow: RECORD + 3 SAVE_STATE calls + HALT."""
    client = NCIClient()
    print(f"NCI version: {client.version()}")
    print(f"Status: {client.get_status()}\n")

    print(f"=== Firing RECORD_REPLAY. Play the game for {duration_s} seconds. ===")
    print("=== 3 SAVE_STATE calls fire at 5s, 10s, 15s into the recording. ===")
    print("=== This mimics SpinLab's reference flow (entrance/cp/exit). ===\n")
    client.record_replay()
    save_marks = {5, 10, 15}
    for s in range(int(duration_s), 0, -1):
        elapsed = int(duration_s) - s
        if elapsed in save_marks:
            print(f"  recording... {s} seconds left | firing SAVE_STATE")
            client.save_state()
        else:
            print(f"  recording... {s} seconds left", end="\r")
        time.sleep(1.0)
    print()
    print("\n=== Firing HALT_REPLAY ===")
    client.halt_replay()
    time.sleep(1.5)
    print("Done. Run with --play to replay. Watch for EOF after buttons.")


def play() -> None:
    client = NCIClient()
    print(f"Status before play: {client.get_status()}")
    print("\n=== Firing PLAY_REPLAY ===")
    print("Watch RA's window for full vs early-EOF playback.")
    client.play_replay()


def usage() -> None:
    print(__doc__)
    print("\nModes:")
    print("  --record-no-saves    record 20s, no SAVE_STATE")
    print("  --record-with-saves  record 20s, 3 SAVE_STATE calls in middle (default)")
    print("  --play               replay whatever RA last recorded")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--record-with-saves"
    if arg == "--play":
        play()
    elif arg == "--record-no-saves":
        record_no_saves()
    elif arg == "--record-with-saves":
        record_with_saves()
    else:
        usage()
        sys.exit(1)
