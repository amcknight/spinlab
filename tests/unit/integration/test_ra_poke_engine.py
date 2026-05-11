"""Tests for RAPokeEngine using a fake NCI client.

The fake holds WRAM in a dict and tracks frame_advance calls. This lets us
verify the engine's poke ordering, held-values behavior, zero-pass, and
detector-wiring without running RetroArch.
"""
from __future__ import annotations

from tests.integration.addresses import ADDR_MAP
from tests.integration.poke_parser import parse_poke
from tests.integration.ra_poke_engine import RAPokeEngine


class FakeNCIClient:
    """Minimal NCI surface for poke-engine tests.

    - read_ram(addr, n): returns wram[addr:addr+n], padded with 0 if missing.
    - write_ram(addr, data): writes bytes into the wram dict.
    - frame_advance(): no-op — the test fixture mutates wram directly to
      simulate ROM behavior between frames if needed; default is "frame
      runs but no CPU writes," matching a paused-by-frame-step model.
    """

    def __init__(self) -> None:
        self.wram: dict[int, int] = {}
        self.writes: list[tuple[int, bytes]] = []
        self.frame_advances = 0

    def read_ram(self, addr: int, n: int = 1) -> bytes:
        return bytes(self.wram.get(addr + i, 0) for i in range(n))

    def write_ram(self, addr: int, data: bytes) -> None:
        for i, b in enumerate(data):
            self.wram[addr + i] = b
        self.writes.append((addr, data))

    def frame_advance(self) -> None:
        self.frame_advances += 1


def test_run_scenario_zeroes_addr_map_before_first_frame():
    fake = FakeNCIClient()
    # Pre-load WRAM with non-zero values that should be cleared by the zero-pass
    for addr in ADDR_MAP.values():
        fake.wram[addr] = 0xFF

    engine = RAPokeEngine(fake)
    scenario = parse_poke("settle: 1\n1: game_mode=20\n")
    engine.run_scenario(scenario)

    # The first ~11 writes (one per ADDR_MAP entry) are the zero-pass.
    zero_writes = fake.writes[: len(ADDR_MAP)]
    written_addrs = {addr for addr, data in zero_writes}
    assert written_addrs == set(ADDR_MAP.values())
    for addr, data in zero_writes:
        assert data == b"\x00", f"zero-pass wrote {data!r} to 0x{addr:04X}"


def test_run_scenario_applies_scheduled_pokes_at_correct_frames():
    fake = FakeNCIClient()
    engine = RAPokeEngine(fake)
    # Frame 1 pokes game_mode=20; frame 3 pokes player_anim=9.
    scenario = parse_poke(
        "settle: 0\n"
        "1: game_mode=20\n"
        "3: player_anim=9\n"
    )
    engine.run_scenario(scenario)

    # By end of run, both held addresses have their correct values.
    assert fake.wram[ADDR_MAP["game_mode"]] == 20
    assert fake.wram[ADDR_MAP["player_anim"]] == 9


def test_held_values_repoke_every_frame():
    fake = FakeNCIClient()
    engine = RAPokeEngine(fake)
    # 2 held bytes, 5 frames total (last_poke=1, settle=4).
    scenario = parse_poke("settle: 4\n1: game_mode=20 level_num=0x05\n")
    engine.run_scenario(scenario)

    # Total writes = 11 (zero-pass) + 2 held bytes × 5 frames = 21.
    expected_total = len(ADDR_MAP) + 2 * 5
    assert len(fake.writes) == expected_total
    # Every frame's writes should include both held addresses.
    held_addrs = {ADDR_MAP["game_mode"], ADDR_MAP["level_num"]}
    post_zero = fake.writes[len(ADDR_MAP):]
    for i in range(0, len(post_zero), 2):
        frame_writes = {addr for addr, _ in post_zero[i:i+2]}
        assert frame_writes == held_addrs


def test_frame_advance_called_once_per_frame():
    fake = FakeNCIClient()
    engine = RAPokeEngine(fake)
    scenario = parse_poke("settle: 4\n1: game_mode=20\n")  # 5 frames
    engine.run_scenario(scenario)
    assert fake.frame_advances == 5


def test_run_scenario_emits_level_entrance_event():
    fake = FakeNCIClient()
    engine = RAPokeEngine(fake)
    scenario = parse_poke(
        "settle: 5\n"
        "1: game_mode=20 level_num=0x105\n"
        "2: level_start=1\n"
    )
    events = engine.run_scenario(scenario)

    from spinlab.protocol import LevelEntranceEvent
    entrance_events = [e for e in events if isinstance(e, LevelEntranceEvent)]
    assert len(entrance_events) == 1
