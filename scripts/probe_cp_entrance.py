"""Live probe to verify the Phase A audit's open question on ADDR_CP_ENTRANCE.

The audit flagged that `ADDR_CP_ENTRANCE = 0x1B403` was read in Mesen via
`emu.read(addr, snesMemory)` — a SNES bus address — but RA's NCI READ_CORE_RAM
takes a WRAM-flat offset. These index different memory.

This probe reads the full transition-detection address set every ~200ms and
prints values + change indicators. Run with RetroArch + a kaizo SMW hack
loaded (Toothpaste World preferred). Walk through a midway tape and a kaizo
ASM-style checkpoint trigger. Watch for `*` on cp_entrance.

If `cp_entrance` changes when you cross an ASM checkpoint: the address is
correct as-is.
If it stays constant or shows random ROM-area noise: the address needs a
different read mechanism (likely READ_CORE_MEMORY with a SNES bus address,
which currently fails on snes9x_libretro with "no memory map defined").
"""
from __future__ import annotations

import time

from spinlab.retroarch.exceptions import NCIError
from spinlab.retroarch.nci import NCIClient

POLL_INTERVAL_SEC = 0.2

ADDRS: dict[str, int] = {
    "level_num":   0x13BF,
    "room":        0x010B,
    "level_start": 0x1935,
    "anim":        0x0071,
    "midway":      0x13CE,
    "cp_entrance": 0x1B403,
}


def fmt(name: str, value: int | None, prev: int | None) -> str:
    val_str = f"0x{value:02X}" if value is not None else " [err]"
    flag = "*" if (prev is not None and value is not None and value != prev) else " "
    return f"{name}={val_str}{flag}"


def main() -> None:
    print(f"Probing {len(ADDRS)} addresses every {int(POLL_INTERVAL_SEC * 1000)}ms.")
    print("Star (*) marks a value that changed since the previous poll.")
    print("Ctrl-C to exit.\n")

    prev: dict[str, int | None] = {n: None for n in ADDRS}

    with NCIClient() as client:
        try:
            print(f"RA version: {client.version()}\n")
        except NCIError as exc:
            print(f"Cannot talk to RA over NCI: {exc}")
            print("Is RetroArch running with network_cmd_enable=true?")
            return

        try:
            while True:
                cells = []
                for name, addr in ADDRS.items():
                    try:
                        value: int | None = client.read_ram(addr, 1)[0]
                    except NCIError:
                        value = None
                    cells.append(fmt(name, value, prev[name]))
                    prev[name] = value
                print("  ".join(cells), flush=True)
                time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\nProbe stopped.")


if __name__ == "__main__":
    main()
