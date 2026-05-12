"""Per-frame poke engine — runs .poke scenarios via NCI against a paused RA.

Each scenario:
  1. Zero every ADDR_MAP byte (per-scenario isolation).
  2. Construct a fresh TransitionDetector.
  3. Run frames in sequence; on each frame:
       a. Apply scheduled pokes for this frame to held_values.
       b. frame_advance() — runs one ROM frame; ROM may overwrite RAM.
       c. Re-write every held byte to overwrite anything the ROM just did
          (fire-and-forget; the synchronous read_snapshot below acts as the
          barrier — read_ram waits for RA to drain its queue before
          responding, so all writes are guaranteed to have landed).
       d. read_snapshot() — acts as implicit sync barrier and reads what
          the detector will see this frame.
       e. detector.step(snap, frame * 16) — emit events.
  4. Terminate when the detector has been quiet for QUIESCENCE_FRAMES past
     the last poke. ``settle_frames`` from the .poke header acts only as
     an upper-bound safety cap; in practice scenarios run a few frames past
     the last event, not the full 60-frame settle.
  5. Return events as list of dicts.

The write-after-frame_advance ordering is load-bearing: when writes happened
*before* frame_advance, ROM-side overwrites of held addresses during the
frame could land in the snapshot, producing flakes like test_orb_exit
intermittently seeing io_port=0 instead of the poked io_port=3.
"""
from __future__ import annotations

from typing import Protocol

from tests.integration.addresses import ADDR_MAP

from spinlab.retroarch.detector import TransitionDetector
from spinlab.retroarch.snapshot import read_snapshot

FRAME_PERIOD_MS = 16  # 60Hz approximation; only used for monotonic timestamps

# Frames of detector silence required to declare a scenario "quiescent"
# (i.e. no more events coming, safe to terminate). 12 frames = 200ms at 60Hz
# — comfortably larger than the longest observed event-firing delay (events
# fire on the same frame as the triggering poke) while still cutting most
# scenarios from ~75 frames to ~30 frames.
QUIESCENCE_FRAMES = 12


class _NCISurface(Protocol):
    def read_ram(self, addr: int, n: int = 1) -> bytes: ...
    def write_ram(self, addr: int, data: bytes) -> None: ...
    def frame_advance(self) -> None: ...


class RAPokeEngine:
    def __init__(self, client: _NCISurface) -> None:
        self._client = client

    def run_scenario(self, scenario: dict) -> list:
        # 1. Zero ADDR_MAP for per-scenario isolation
        for addr in ADDR_MAP.values():
            self._client.write_ram(addr, b"\x00")

        # 2. Schedule + bookkeeping
        schedule: dict[int, list[dict]] = {}
        for poke in scenario["pokes"]:
            schedule.setdefault(poke["frame"], []).append(poke)
        last_poke_frame = max(schedule, default=0)
        end_frame_cap = last_poke_frame + scenario["settle_frames"]

        held: dict[int, int] = {}
        detector = TransitionDetector()
        events: list = []
        # Start the quiescence clock at last_poke_frame so we always run at
        # least QUIESCENCE_FRAMES past the last write before terminating —
        # gives the detector room to observe the final state changes.
        frame_of_last_event = last_poke_frame

        for frame in range(1, end_frame_cap + 1):
            for poke in schedule.get(frame, []):
                held[poke["addr"]] = poke["value"]
            self._client.frame_advance()
            # Re-assert held values AFTER the ROM frame ran. Mask to low byte:
            # .poke files use values like 0x105 (e.g., level_num) — only the
            # low byte lands at $13BF; the high byte is held separately on
            # $13BE in the actual ROM.
            for addr, value in held.items():
                self._client.write_ram(addr, bytes([value & 0xFF]))
            snap = read_snapshot(self._client)  # type: ignore[arg-type]
            new_events = list(detector.step(snap, frame * FRAME_PERIOD_MS))
            events.extend(new_events)
            if new_events:
                frame_of_last_event = frame

            # Quiescence-based early termination. Don't even check until we've
            # passed last_poke_frame so all scheduled pokes have a chance to
            # land before we declare the scenario done.
            if (frame > last_poke_frame
                    and frame - frame_of_last_event >= QUIESCENCE_FRAMES):
                break

        return events
