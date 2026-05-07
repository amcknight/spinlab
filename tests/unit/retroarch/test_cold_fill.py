from spinlab.retroarch.cold_fill import ColdFillTracker
from spinlab.retroarch.events import Spawn
from spinlab.retroarch.snapshot import MemorySnapshot


def _snap(**ov) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(ov)
    return MemorySnapshot(**base)


def test_inactive_emits_nothing():
    cf = ColdFillTracker()
    assert cf.step(_snap(player_anim=9), timestamp_ms=0) is None


def test_active_waits_for_death_then_spawn():
    cf = ColdFillTracker()
    cf.activate(segment_id="boss-1")

    # Pre-death: nothing.
    assert cf.step(_snap(player_anim=0, level_start=1), timestamp_ms=0) is None
    # Death detected: still nothing emitted yet.
    assert cf.step(_snap(player_anim=9, level_start=1), timestamp_ms=16) is None
    # Still dying.
    assert cf.step(_snap(player_anim=9, level_start=0), timestamp_ms=32) is None
    # Spawn: level_start 0 -> 1 -> emits Spawn, deactivates.
    e = cf.step(_snap(player_anim=0, level_start=1), timestamp_ms=48)
    assert isinstance(e, Spawn)
    assert e.is_cold_cp is True
    assert e.state_captured is True
    assert cf.is_active() is False


def test_fast_retry_path():
    """level_start stays at 1; spawn detected via player_anim 9 -> not-9."""
    cf = ColdFillTracker()
    cf.activate(segment_id="x")

    cf.step(_snap(player_anim=0, level_start=1), timestamp_ms=0)
    cf.step(_snap(player_anim=9, level_start=1), timestamp_ms=16)
    e = cf.step(_snap(player_anim=0, level_start=1), timestamp_ms=32)
    assert isinstance(e, Spawn)
