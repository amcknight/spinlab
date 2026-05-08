"""Direct test: does NCI SAVE_STATE actually write a file?

Bypasses the dashboard / state_io entirely. Snapshots the RA savestate
directory, sends a single SAVE_STATE over UDP, waits 2 seconds, lists
what's new. Run with RA up and a ROM loaded.

If a new <game>.stateN file appears: NCI save works; the dashboard logic
must be wrong.
If nothing appears: NCI save is broken in this RA install — F2 fires
through a different code path. We need to identify what RA flag / version
gates the network command.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from spinlab.retroarch.exceptions import NCIError
from spinlab.retroarch.nci import NCIClient


def main() -> int:
    # Prompt: where does RA write savestates? Adjust if your config differs.
    save_dir = Path(r"C:\RetroArch-Win64\states\Snes9x")
    if not save_dir.is_dir():
        print(f"ERROR: savestate dir not found: {save_dir}")
        return 1

    # 1. Snapshot.
    before = {p.name: p.stat().st_mtime for p in save_dir.glob("*.state*") if p.is_file()}
    print(f"snapshot: {len(before)} files matching *.state* in {save_dir}")
    if before:
        recent = max(before.items(), key=lambda kv: kv[1])
        print(f"most-recent: {recent[0]} (mtime={recent[1]:.2f})")

    # 2. Probe NCI is alive.
    client = NCIClient(timeout=1.0)
    try:
        ver = client.version()
        print(f"NCI VERSION: {ver}")
    except NCIError as exc:
        print(f"ERROR: NCI not reachable: {exc}")
        return 1

    # 3. Ask RA what game it has loaded.
    try:
        status = client.get_status()
        print(f"GET_STATUS: state={status.state} game={status.game!r} crc32={status.crc32}")
    except NCIError as exc:
        print(f"WARNING: GET_STATUS failed: {exc}")

    # 4. Fire SAVE_STATE.
    print("\n>>> sending SAVE_STATE...")
    client.save_state()
    print("    sent (UDP fire-and-forget)")

    # 5. Wait, then snapshot again.
    print("    waiting 2s for any file to appear/advance...")
    time.sleep(2.0)

    after = {p.name: p.stat().st_mtime for p in save_dir.glob("*.state*") if p.is_file()}

    new_or_changed = []
    for name, cur_mtime in after.items():
        old_mtime = before.get(name)
        if old_mtime is None:
            new_or_changed.append((name, "NEW"))
        elif cur_mtime > old_mtime:
            new_or_changed.append((name, "MODIFIED"))

    print(f"\nafter: {len(after)} files (was {len(before)})")
    if new_or_changed:
        print("CHANGES:")
        for name, kind in new_or_changed:
            print(f"  {kind}: {name}")
        print("\nVERDICT: NCI SAVE_STATE works. The dashboard logic is the problem.")
    else:
        print("CHANGES: none.")
        print("\nVERDICT: NCI SAVE_STATE does NOT trigger a save in this RA install.")
        print("That's the Phase 0 INCONCLUSIVE finding — F2 manual works, NCI doesn't.")
        print("Try F2 manually right now and re-run this script to confirm asymmetry.")

    client.close()
    return 0 if new_or_changed else 2


if __name__ == "__main__":
    sys.exit(main())
