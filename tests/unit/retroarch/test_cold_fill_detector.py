from spinlab.protocol import SpawnEvent
from spinlab.retroarch.cold_fill_detector import ColdFillSpawnDetector
from spinlab.retroarch.snapshot import MemorySnapshot


def _snap(**ov) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(ov)
    return MemorySnapshot(**base)


def test_inactive_emits_nothing():
    cf = ColdFillSpawnDetector()
    assert cf.step(_snap(player_anim=9), timestamp_ms=0) is None


def test_active_waits_for_death_then_spawn():
    cf = ColdFillSpawnDetector()
    cf.activate(segment_id="boss-1")

    # Pre-death: nothing.
    assert cf.step(_snap(player_anim=0, level_start=1), timestamp_ms=0) is None
    # Death detected: still nothing emitted yet.
    assert cf.step(_snap(player_anim=9, level_start=1), timestamp_ms=16) is None
    # Still dying.
    assert cf.step(_snap(player_anim=9, level_start=0), timestamp_ms=32) is None
    # Spawn: level_start 0 -> 1 -> emits Spawn, deactivates.
    e = cf.step(_snap(player_anim=0, level_start=1), timestamp_ms=48)
    assert isinstance(e, SpawnEvent)
    assert e.is_cold_cp is True
    assert cf.is_active() is False


def test_fast_retry_path():
    """level_start stays at 1; spawn detected via player_anim 9 -> not-9."""
    cf = ColdFillSpawnDetector()
    cf.activate(segment_id="x")

    cf.step(_snap(player_anim=0, level_start=1), timestamp_ms=0)
    cf.step(_snap(player_anim=9, level_start=1), timestamp_ms=16)
    e = cf.step(_snap(player_anim=0, level_start=1), timestamp_ms=32)
    assert isinstance(e, SpawnEvent)


def test_pit_fall_death_via_exit_mode_then_spawn():
    """Real-world: many SMW deaths skip anim=9 entirely (Mario falls off
    screen). The only signal is exit_mode going non-zero with no goal flag.
    Without this path, pit-falls in cold-fill never advance to spawn-watch."""
    cf = ColdFillSpawnDetector()
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

    assert isinstance(e, SpawnEvent), \
        "pit-fall death should advance through to spawn capture"
    assert e.is_cold_cp is True
    assert e.segment_id == "seg-pit"


def test_cp_respawn_via_playable_check():
    """Regression: Love Yourself, 2026-05-07. Dying with a CP set respawns
    the player at the CP without reloading the level, so the MARIO-START
    splash never fires. Neither $1935 (level_start) nor $0100 (game_mode)
    gives a stable hack-independent "in level" signal — both differ by
    sublevel:

      Level 44 (Love Yourself):   level_start=8,  game_mode=20
      Level 58 (Love Yourself):   level_start=22, game_mode=22

    edge_spawn / fast_retry both gate on level_start == 1 and miss CP
    respawn entirely. The simpler reliable check: once _waiting_spawn is
    set (we KNOW a death fired), fire on the first frame where exit_mode
    is back to 0 and player_anim is not the death sprite — i.e., the death
    sequence is over. No level-identification needed."""
    # Level 58 scenario: game_mode=22, level_start=22, exit_mode flips
    # 0 -> 6 -> 9 -> 0 across the death-respawn sequence.
    cf = ColdFillSpawnDetector()
    cf.activate(segment_id="seg-58")

    cf.step(_snap(game_mode=22, player_anim=1, level_start=22, exit_mode=0),
            timestamp_ms=0)
    # Pit fall — exit_mode goes non-zero, no goal flags.
    cf.step(_snap(game_mode=22, player_anim=1, level_start=22, exit_mode=6),
            timestamp_ms=16)
    # Death sequence playing out.
    cf.step(_snap(game_mode=22, player_anim=1, level_start=22, exit_mode=9),
            timestamp_ms=32)
    # Player respawns at CP — exit_mode back to 0, anim still alive.
    e = cf.step(_snap(game_mode=22, player_anim=1, level_start=22, exit_mode=0),
                timestamp_ms=48)
    assert isinstance(e, SpawnEvent), \
        "L58 cp-respawn must capture once exit_mode returns to 0 (anim alive)"
    assert e.segment_id == "seg-58"

    # Level 44 scenario: same logic, different stale bytes.
    cf2 = ColdFillSpawnDetector()
    cf2.activate(segment_id="seg-44")
    cf2.step(_snap(game_mode=20, player_anim=0, level_start=8, exit_mode=0),
             timestamp_ms=0)
    cf2.step(_snap(game_mode=20, player_anim=0, level_start=8, exit_mode=1),
             timestamp_ms=16)
    e2 = cf2.step(_snap(game_mode=20, player_anim=0, level_start=8, exit_mode=0),
                  timestamp_ms=32)
    assert isinstance(e2, SpawnEvent), \
        "L44 cp-respawn must capture once exit_mode returns to 0 (anim alive)"
    assert e2.segment_id == "seg-44"


def test_resync_after_state_load_suppresses_phantom_death():
    """Regression: Love Yourself L58, 2026-05-08. After cold-fill loads a hot
    state whose saved exit_mode != 0 (or anim != 0), the very next poll fed
    raw to step() saw prev=0 (from activate()) and curr=loaded-value, firing
    died_via_exit instantly as a phantom death — detector entered
    _waiting_spawn=True before the player had touched the controller. Then
    the player wasn't playing (the hot state was the wrong segment) so the
    detector hung forever.

    The poller calls resync_after_state_load(snap) after every state load,
    syncing prev_* to the loaded snapshot so the very first step() sees
    prev==curr and emits no edge."""
    cf = ColdFillSpawnDetector()
    cf.activate(segment_id="seg")

    # Loaded hot state has exit_mode=1 (any non-zero — saved at a moment
    # mid-transition). Without resync, this would phantom-fire died_via_exit.
    loaded_snap = _snap(player_anim=1, level_start=22, exit_mode=1)
    cf.resync_after_state_load(loaded_snap)

    # First step after resync sees curr==prev — no edge, no phantom.
    e = cf.step(loaded_snap, timestamp_ms=0)
    assert e is None
    assert cf._waiting_spawn is False, \
        "phantom died_via_exit should be suppressed after resync"

    # Player actually plays now: exit_mode goes 1 -> 0 -> 1 (real death).
    cf.step(_snap(player_anim=1, level_start=22, exit_mode=0), timestamp_ms=16)
    cf.step(_snap(player_anim=1, level_start=22, exit_mode=6), timestamp_ms=32)
    assert cf._waiting_spawn is True, "real death should still be detected"


def test_no_false_positive_when_active_but_not_yet_dead():
    """ColdFill activates with player already in playable state (just loaded
    the hot CP state). We must NOT emit Spawn before any death is detected —
    that would capture the same hot state we just loaded as the cold state."""
    cf = ColdFillSpawnDetector()
    cf.activate(segment_id="seg")

    # Many frames of playable state, no death yet.
    for t in range(0, 1000, 16):
        e = cf.step(_snap(player_anim=0, level_start=1, exit_mode=0), timestamp_ms=t)
        assert e is None, f"false-positive Spawn at t={t} before any death"
    assert cf.is_active() is True


def test_trace_logs_on_change_and_is_silent_on_repeat(caplog):
    import logging
    from spinlab.retroarch.cold_fill_detector import ColdFillSpawnDetector
    det = ColdFillSpawnDetector()
    det.activate("seg1")
    snap = _snap(player_anim=0, exit_mode=0, level_start=1)
    with caplog.at_level(logging.INFO, logger="spinlab.retroarch.cold_fill_detector"):
        det.step(snap, timestamp_ms=0)
        first = [r for r in caplog.records if "trace" in r.getMessage()]
        det.step(snap, timestamp_ms=16)  # identical signal → no new trace line
        second = [r for r in caplog.records if "trace" in r.getMessage()]
    assert len(first) == 1
    assert len(second) == 1  # unchanged → no additional trace


def test_goal_exit_does_not_count_as_death():
    """exit_mode change WITH a goal signal (fanfare or io_port=goal/orb/key)
    is a normal level completion, not a death. Cold-fill must not treat it
    as a death indicator."""
    from spinlab.retroarch import addresses as a

    cf = ColdFillSpawnDetector()
    cf.activate(segment_id="seg-goal")

    cf.step(_snap(player_anim=0, level_start=1, exit_mode=0), timestamp_ms=0)
    # Mario hits goal tape: exit_mode goes non-zero AND fanfare lights up.
    cf.step(_snap(player_anim=0, level_start=1, exit_mode=1, fanfare=1), timestamp_ms=16)
    # Should still be waiting for death, not for spawn.
    assert cf._waiting_spawn is False, \
        "goal exit should not be misclassified as death"

    # Same for orb/key:
    cf2 = ColdFillSpawnDetector()
    cf2.activate(segment_id="seg-orb")
    cf2.step(_snap(player_anim=0, level_start=1, exit_mode=0), timestamp_ms=0)
    cf2.step(_snap(player_anim=0, level_start=1, exit_mode=1, io_port=a.IO_ORB), timestamp_ms=16)
    assert cf2._waiting_spawn is False
