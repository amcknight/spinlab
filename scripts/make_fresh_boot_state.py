"""Generate a "fresh boot" savestate for a ROM in tests/integration/states/.

Run once per ROM in tests/integration/conftest.py:ROM_REGISTRY. The resulting
.state file is committed to the repo and loaded by RAPokeEngine before each
scenario, replacing the session-scoped ROM CPU/SPC state leak that flakes
test_key_exit / test_orb_exit (see
docs/superpowers/plans/2026-05-14-transition-state-leak-followup.md).

Usage:
    python scripts/make_fresh_boot_state.py --rom-key vanilla_smw
    python scripts/make_fresh_boot_state.py --rom-key love_yourself
    python scripts/make_fresh_boot_state.py --all
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Project paths.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "python"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.integration.conftest import (  # noqa: E402
    ROM_REGISTRY,
    _free_udp_port,
    _resolve_ra_paths,
)
from tests.integration.ra_harness import RAHarness  # noqa: E402

# Output dir for committed savestates.
STATES_DIR = PROJECT_ROOT / "tests" / "integration" / "states"

# Frames of free-run after RESET that we use to settle the ROM past its title
# screen / intro animation, before snapshotting the savestate. 600 frames =
# ~10 seconds at 60 Hz — empirically enough to land on a stable in-game frame
# for SMW and SMW romhacks. Increase if a ROM has a longer intro.
SETTLE_FRAMES = 600

# RA's snes9x core writes to <savestate_directory>/<core_subdir>/<rom>.state{N}.
# Slot 9999 is reserved by the spinlab dashboard for replay slot navigation;
# pick a different "scratch" slot for fresh-boot creation.
FRESH_BOOT_SLOT = 9998

# Sleep between SAVE_STATE and reading the file off disk. snes9x writes
# the savestate synchronously from the main thread, so a short wait is
# enough to ensure the file exists.
SAVE_DRAIN_SEC = 1.0


def make_fresh_boot_state(rom_key: str) -> Path:
    """Launch RA on ``rom_key``, run past boot, snapshot savestate, return its path.

    Returns the path the savestate was written to under STATES_DIR.
    """
    retroarch_exe, ra_core_path, rom_path = _resolve_ra_paths(rom_key)

    # Per-launch temp dir for the savestate so we don't pollute the user's
    # actual savestate directory.
    tmp_savestate_dir = Path(tempfile.mkdtemp(prefix="spinlab_fresh_boot_"))
    # snes9x writes to <savestate_directory>/Snes9x/<rom>.state{N}, so the
    # file ends up nested one level under the dir we configure.
    snes9x_subdir = tmp_savestate_dir / "Snes9x"
    snes9x_subdir.mkdir(parents=True, exist_ok=True)

    extra_cfg = (
        f'savestate_directory = "{tmp_savestate_dir.as_posix()}"\n'
        f'state_slot = "{FRESH_BOOT_SLOT}"\n'
    )

    print(f"[{rom_key}] launching RA on {rom_path.name}...")
    harness = RAHarness.launch(
        rom_path=rom_path,
        core_path=ra_core_path,
        retroarch_exe=retroarch_exe,
        extra_cfg=extra_cfg,
        nci_port=_free_udp_port(),
    )
    try:
        # RA is paused after launch (per harness invariant). Free-run to settle
        # past boot.
        client = harness.client
        print(f"[{rom_key}] unpausing + free-running {SETTLE_FRAMES} frames...")
        client.pause_toggle()
        # Free-running for SETTLE_FRAMES frames at 60 Hz = SETTLE_FRAMES/60 seconds.
        time.sleep(SETTLE_FRAMES / 60.0)
        client.pause_toggle()
        # Brief settle so the pause-toggle has been applied before we save.
        time.sleep(0.3)

        print(f"[{rom_key}] SAVE_STATE to slot {FRESH_BOOT_SLOT}...")
        client.save_state()
        time.sleep(SAVE_DRAIN_SEC)

        # snes9x writes <rom>.state{N}. RA auto-advances state_slot at runtime
        # ("savestate_auto_index"), so the appendconfig's state_slot setting
        # does not necessarily land — RA picks the next free slot. Since the
        # tmp savestate dir starts empty, exactly one .state* file should
        # appear; take whichever it is rather than pinning the slot number.
        rom_basename = rom_path.stem
        candidates = sorted(snes9x_subdir.glob(f"{rom_basename}.state*"))
        if not candidates:
            existing = list(snes9x_subdir.iterdir())
            raise RuntimeError(
                f"[{rom_key}] no savestate written under {snes9x_subdir} "
                f"(dir contents: {[p.name for p in existing]})"
            )
        if len(candidates) > 1:
            raise RuntimeError(
                f"[{rom_key}] multiple savestate candidates in {snes9x_subdir} — "
                f"unexpected (dir was created empty): "
                f"{[p.name for p in candidates]}"
            )
        src = candidates[0]

        STATES_DIR.mkdir(parents=True, exist_ok=True)
        dst = STATES_DIR / f"{rom_basename}.state"
        shutil.copyfile(src, dst)
        print(f"[{rom_key}] wrote {dst} ({dst.stat().st_size} bytes)")
        return dst
    finally:
        harness.teardown()
        shutil.rmtree(tmp_savestate_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--rom-key",
        help="Single rom_key from ROM_REGISTRY (e.g. 'default', 'love_yourself').",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Generate a savestate for every rom_key in ROM_REGISTRY.",
    )
    args = parser.parse_args()

    if args.all:
        # De-dup by ROM filename so we don't snapshot the same physical ROM
        # twice when two registry keys point at it.
        seen_rom: set[str] = set()
        keys: list[str] = []
        for k, fn in ROM_REGISTRY.items():
            if fn in seen_rom:
                print(f"[{k}] skipping (already snapshotted via earlier key for {fn})")
                continue
            seen_rom.add(fn)
            keys.append(k)
    else:
        if args.rom_key not in ROM_REGISTRY:
            print(f"unknown rom_key {args.rom_key!r}; known: {sorted(ROM_REGISTRY)}")
            return 2
        keys = [args.rom_key]

    failures: list[tuple[str, Exception]] = []
    for k in keys:
        try:
            make_fresh_boot_state(k)
        except Exception as exc:  # noqa: BLE001 — surfacing failure per-rom
            print(f"[{k}] FAILED: {exc}")
            failures.append((k, exc))

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for k, exc in failures:
            print(f"  [{k}] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
