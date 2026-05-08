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


def test_pit_fall_death_via_exit_mode_then_spawn():
    """Real-world: many SMW deaths skip anim=9 entirely (Mario falls off
    screen). The only signal is exit_mode going non-zero with no goal flag.
    Without this path, pit-falls in cold-fill never advance to spawn-watch."""
    cf = ColdFillTracker()
    cf.activate(segment_id="seg-pit")

    # Playing — exit_mode 0, anim 0.
    cf.step(_snap(player_anim=0, level_start=1, exit_mode=0), timestamp_ms=0)
    # Mario falls in a pit: exit_mode 0 -> non-zero, no goal/orb/key/fanfare.
    cf.step(_snap(player_anim=0, level_start=1, exit_mode=1), timestamp_ms=16)
    # Now waiting for spawn.
    # Level transitions, level_start 1 -> 0 briefly.
    cf.step(_snap(player_anim=0, level_start=0, exit_mode=1), timestamp_ms=32)
    # Respawn: level_start back to 1.
    e = cf.step(_snap(player_anim=0, level_start=1, exit_mode=0), timestamp_ms=48)

    assert isinstance(e, Spawn), \
        "pit-fall death should advance through to spawn capture"
    assert e.is_cold_cp is True
    assert e.segment_id == "seg-pit"


def test_cp_respawn_via_playable_check():
    """In some SMW hacks, dying with a CP set just respawns the player at the
    CP — the level isn't reloaded, so level_start stays at 1 throughout.
    edge_spawn (level_start 0->1) never fires, and many deaths skip anim=9 too,
    so fast_retry doesn't fire either.

    Once cold-fill is in waiting_spawn (we KNOW a death fired), fire on the
    first frame where the player is back in playable state (exit_mode=0,
    level_start=1, anim != 9). Level-triggered, not edge-triggered, so we
    don't miss the conjunction-not-coinciding case.

    Without this path, cold-fill in such hacks gets stuck in waiting_spawn
    forever (observed: 5 LevelExits, 0 Spawns)."""
    cf = ColdFillTracker()
    cf.activate(segment_id="seg-cp")

    # Playing — exit_mode 0, level_start 1, anim 0.
    cf.step(_snap(player_anim=0, level_start=1, exit_mode=0), timestamp_ms=0)
    # Sprite hit / pit fall — exit_mode goes non-zero, no goal flags.
    cf.step(_snap(player_anim=0, level_start=1, exit_mode=1), timestamp_ms=16)
    # Death sequence playing out.
    cf.step(_snap(player_anim=0, level_start=1, exit_mode=1), timestamp_ms=32)
    # Player respawns at CP — back to playable.
    e = cf.step(_snap(player_anim=0, level_start=1, exit_mode=0), timestamp_ms=48)

    assert isinstance(e, Spawn), \
        "cp-respawn must capture spawn once player is back in playable state"
    assert e.is_cold_cp is True
    assert e.segment_id == "seg-cp"


def test_no_false_positive_when_active_but_not_yet_dead():
    """ColdFill activates with player already in playable state (just loaded
    the hot CP state). We must NOT emit Spawn before any death is detected —
    that would capture the same hot state we just loaded as the cold state."""
    cf = ColdFillTracker()
    cf.activate(segment_id="seg")

    # Many frames of playable state, no death yet.
    for t in range(0, 1000, 16):
        e = cf.step(_snap(player_anim=0, level_start=1, exit_mode=0), timestamp_ms=t)
        assert e is None, f"false-positive Spawn at t={t} before any death"
    assert cf.is_active() is True


def test_goal_exit_does_not_count_as_death():
    """exit_mode change WITH a goal signal (fanfare or io_port=goal/orb/key)
    is a normal level completion, not a death. Cold-fill must not treat it
    as a death indicator."""
    from spinlab.retroarch import addresses as a

    cf = ColdFillTracker()
    cf.activate(segment_id="seg-goal")

    cf.step(_snap(player_anim=0, level_start=1, exit_mode=0), timestamp_ms=0)
    # Mario hits goal tape: exit_mode goes non-zero AND fanfare lights up.
    cf.step(_snap(player_anim=0, level_start=1, exit_mode=1, fanfare=1), timestamp_ms=16)
    # Should still be waiting for death, not for spawn.
    assert cf._waiting_spawn is False, \
        "goal exit should not be misclassified as death"

    # Same for orb/key:
    cf2 = ColdFillTracker()
    cf2.activate(segment_id="seg-orb")
    cf2.step(_snap(player_anim=0, level_start=1, exit_mode=0), timestamp_ms=0)
    cf2.step(_snap(player_anim=0, level_start=1, exit_mode=1, io_port=a.IO_ORB), timestamp_ms=16)
    assert cf2._waiting_spawn is False
