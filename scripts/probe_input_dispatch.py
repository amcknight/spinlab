"""Probe RetroArch's input dispatch paths to settle the NCI hotkey-sim issue.

Three concentrated tests:
  A. NCI MENU_TOGGLE — settle cheevos hardcore mode theory.
  B. NCI SAVE_STATE — confirm whether hotkey-sim works after cheevos fix.
  C. Network RetroPad RIGHT input — confirm whether NRP input reaches the game.

Prerequisites (cfg edits + RA restart):
  - network_cmd_enable = "true"
  - network_remote_enable = "true"
  - network_remote_enable_user_p1 = "true"
  - cheevos_hardcore_mode_enable = "false"
  - run_ahead_enabled = "false"   (rule out runahead confound)

Then load Toothpaste and get into a level with Mario movable.

Run from worktree root:  python scripts/probe_input_dispatch.py
"""
from __future__ import annotations

import socket
import struct
import sys
import time
from pathlib import Path

# Allow running without install
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from spinlab.retroarch.nci import NCIClient  # noqa: E402

NCI_HOST = ("127.0.0.1", 55355)
NRP_HOST = ("127.0.0.1", 55400)
STATES_DIR = Path("C:/RetroArch-Win64/states/Snes9x")

ADDR_MARIO_X = 0x0094  # SMW Mario X position (16-bit)
ADDR_FRAME_COUNTER = 0x0013

# Network RetroPad packet format. Matches C struct on x64 with default padding:
#   struct remote_message { int port; int device; int index; int id; uint16_t state; }
# All four ints are 4 bytes (16 total), state is 2 bytes, then 2 bytes of trailing
# padding to round to 4-byte struct alignment. Total: 20 bytes, little-endian on Win.
NRP_STRUCT = struct.Struct("<iiiiH2x")

RETRO_DEVICE_JOYPAD = 1
BUTTON_RIGHT = 7
BUTTON_A = 8


def send_nrp(port: int, button_id: int, pressed: bool) -> None:
    """Send a single Network RetroPad message."""
    packet = NRP_STRUCT.pack(port, RETRO_DEVICE_JOYPAD, 0, button_id, 1 if pressed else 0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(packet, NRP_HOST)
    finally:
        sock.close()


def header(s: str) -> None:
    print(f"\n{'=' * 60}\n {s}\n{'=' * 60}")


def info(s: str) -> None:
    print(f"  {s}")


def test_a_menu_toggle() -> None:
    header("Test A: NCI MENU_TOGGLE (visual check)")
    info("Sending MENU_TOGGLE via NCI...")
    info("WATCH THE RA WINDOW — does the menu open?")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(b"MENU_TOGGLE", NCI_HOST)
    sock.close()
    info("...waiting 3s; if menu opened, the script will toggle it closed next.")
    time.sleep(3)
    info("Sending MENU_TOGGLE again (closes menu if it was open).")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(b"MENU_TOGGLE", NCI_HOST)
    sock.close()


def test_b_save_state(client: NCIClient) -> None:
    header("Test B: NCI SAVE_STATE — does a state file appear?")
    if not STATES_DIR.exists():
        info(f"states dir missing: {STATES_DIR}")
        return
    before = {p.name for p in STATES_DIR.glob("*.state*")}
    info(f"slots before: {len(before)} files")
    info("Sending SAVE_STATE...")
    client.save_state()
    time.sleep(1.0)
    after = {p.name for p in STATES_DIR.glob("*.state*")}
    new = sorted(after - before)
    if new:
        info(f"NEW FILE(S): {new}  -> NCI SAVE_STATE works!")
    else:
        info("no new file. NCI SAVE_STATE silently dropped.")


def test_c_network_retropad(client: NCIClient) -> None:
    header("Test C: Network RetroPad — does pressing RIGHT move Mario?")
    info(f"Reading Mario X position before...")
    pre = client.read_ram(ADDR_MARIO_X, 2)
    pre_x = pre[0] | (pre[1] << 8)
    info(f"  before: {pre_x}")

    info("Sending RIGHT pressed for 30 frames (~0.5s) via Network RetroPad...")
    # We need to keep the button "pressed" across multiple input polls.
    # RA reads NRP each frame, so we send a "pressed" packet every ~16ms.
    deadline = time.perf_counter() + 0.5
    while time.perf_counter() < deadline:
        send_nrp(port=0, button_id=BUTTON_RIGHT, pressed=True)
        time.sleep(0.016)
    # Release
    send_nrp(port=0, button_id=BUTTON_RIGHT, pressed=False)
    time.sleep(0.2)

    post = client.read_ram(ADDR_MARIO_X, 2)
    post_x = post[0] | (post[1] << 8)
    info(f"  after:  {post_x}")

    delta = post_x - pre_x
    if abs(delta) > 4:
        info(f"Mario X changed by {delta} pixels -> Network RetroPad input REACHES the game.")
    else:
        info(f"Mario X barely changed ({delta}). NRP either not received, or was blocked.")


def main() -> int:
    print("Input dispatch probe — three tests")
    print("Pre-reqs: cfg edited per docs/retroarch-migration/spike-log.md, RA restarted, in a level")
    time.sleep(1)

    with NCIClient() as client:
        # Sanity check first
        try:
            v = client.version()
            info(f"NCI alive: VERSION = {v!r}")
        except Exception as e:
            print(f"NCI not reachable: {e}")
            return 1

        test_a_menu_toggle()
        test_b_save_state(client)
        test_c_network_retropad(client)

    print("\nDone. Tell me:")
    print("  A. Did the RA menu open visibly when MENU_TOGGLE was sent?")
    print("  B. Was a new state file created? (script reports above)")
    print("  C. Did Mario X advance? (script reports above)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
