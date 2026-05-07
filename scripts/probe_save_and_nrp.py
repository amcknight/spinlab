"""Slimmer follow-up probe: only NCI SAVE_STATE and Network RetroPad input.

Run AFTER ensuring the RA menu is closed (manual F1/Esc/B-on-gamepad if open).
Game must be in a level with Mario movable; no input from the user during the
test (so we can attribute Mario X changes to the probe, not human input).
"""
from __future__ import annotations

import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from spinlab.retroarch.nci import NCIClient  # noqa: E402

NRP_HOST = ("127.0.0.1", 55400)
STATES_DIR = Path("C:/RetroArch-Win64/states/Snes9x")
ADDR_MARIO_X = 0x0094

NRP_STRUCT = struct.Struct("<iiiiH2x")
RETRO_DEVICE_JOYPAD = 1
BUTTON_RIGHT = 7


def send_nrp(button_id: int, pressed: bool) -> None:
    packet = NRP_STRUCT.pack(0, RETRO_DEVICE_JOYPAD, 0, button_id, 1 if pressed else 0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(packet, NRP_HOST)
    finally:
        sock.close()


def main() -> int:
    with NCIClient() as client:
        try:
            print(f"NCI alive: VERSION = {client.version()!r}")
        except Exception as e:
            print(f"NCI not reachable: {e}")
            return 1

        # Sanity: confirm the core is actually running (not paused / not in menu).
        running = client.is_core_running(tick_addr=0x0013, sample_delay=0.1)
        print(f"core_running = {running} (must be True; if False, the menu is open or RA is paused)")
        if not running:
            print("ABORT: emulator is paused. Close the menu (F1/Esc) and re-run.")
            return 2

        # --- Test B: NCI SAVE_STATE ---
        print("\n--- Test B: NCI SAVE_STATE ---")
        before = {p.name for p in STATES_DIR.glob("*.state*")}
        print(f"slots before: {len(before)} files")
        client.save_state()
        time.sleep(1.0)
        after = {p.name for p in STATES_DIR.glob("*.state*")}
        new = sorted(after - before)
        if new:
            print(f"  PASS  new file(s): {new}")
        else:
            print(f"  FAIL  no new state file appeared")

        # --- Test C: Network RetroPad RIGHT ---
        print("\n--- Test C: Network RetroPad RIGHT for 0.5s ---")
        pre = client.read_ram(ADDR_MARIO_X, 2)
        pre_x = pre[0] | (pre[1] << 8)
        print(f"  Mario X before: {pre_x}")

        deadline = time.perf_counter() + 0.5
        while time.perf_counter() < deadline:
            send_nrp(BUTTON_RIGHT, pressed=True)
            time.sleep(0.016)
        send_nrp(BUTTON_RIGHT, pressed=False)
        time.sleep(0.2)

        post = client.read_ram(ADDR_MARIO_X, 2)
        post_x = post[0] | (post[1] << 8)
        print(f"  Mario X after:  {post_x}")
        delta = post_x - pre_x
        if abs(delta) > 4:
            print(f"  PASS  Mario advanced by {delta} pixels — Network RetroPad input reaches the game")
        else:
            print(f"  FAIL  Mario barely changed ({delta}). NRP not received or input blocked.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
