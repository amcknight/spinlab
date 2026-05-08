"""Per-frame poke engine — runs .poke scenarios via NCI against a paused RA.

Each scenario:
  1. Zero every ADDR_MAP byte (per-scenario isolation).
  2. Construct a fresh TransitionDetector.
  3. For each frame in 1..(last_poke_frame + settle_frames):
       a. Apply scheduled pokes for this frame to held_values.
       b. Re-write every held byte (fire-and-forget).
       c. frame_advance().
       d. read_snapshot() — acts as implicit sync barrier.
       e. detector.step(snap, frame * 16) — emit events.
  4. Return events as list of dicts.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Protocol

from spinlab.retroarch.detector import TransitionDetector
from spinlab.retroarch.snapshot import read_snapshot
from tests.integration.addresses import ADDR_MAP

FRAME_PERIOD_MS = 16  # 60Hz approximation; only used for monotonic timestamps


class _NCISurface(Protocol):
    def read_ram(self, addr: int, n: int = 1) -> bytes: ...
    def write_ram(self, addr: int, data: bytes) -> None: ...
    def frame_advance(self) -> None: ...


class RAPokeEngine:
    def __init__(self, client: _NCISurface) -> None:
        self._client = client

    def run_scenario(self, scenario: dict) -> list[dict]:
        # 1. Zero ADDR_MAP for per-scenario isolation
        for addr in ADDR_MAP.values():
            self._client.write_ram(addr, b"\x00")

        # 2. Schedule + bookkeeping
        schedule: dict[int, list[dict]] = {}
        for poke in scenario["pokes"]:
            schedule.setdefault(poke["frame"], []).append(poke)
        last_poke_frame = max(schedule, default=0)
        end_frame = last_poke_frame + scenario["settle_frames"]

        held: dict[int, int] = {}
        detector = TransitionDetector()
        events: list[dict] = []

        for frame in range(1, end_frame + 1):
            for poke in schedule.get(frame, []):
                held[poke["addr"]] = poke["value"]
            # Mask to low byte: matches Lua's emu.write semantics, which writes
            # one byte and silently truncates wider values. .poke files use
            # values like 0x105 (e.g., level_num) — Lua writes 0x05 to $13BF.
            for addr, value in held.items():
                self._client.write_ram(addr, bytes([value & 0xFF]))
            self._client.frame_advance()
            snap = read_snapshot(self._client)  # type: ignore[arg-type]
            for ev in detector.step(snap, frame * FRAME_PERIOD_MS):
                events.append(asdict(ev))

        return events
