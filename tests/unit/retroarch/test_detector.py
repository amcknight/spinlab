"""TransitionDetector tests — drive sequences of synthetic snapshots through it."""
from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)
from spinlab.retroarch.detector import TransitionDetector
from spinlab.retroarch.snapshot import MemorySnapshot


def _snap(**overrides) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(overrides)
    return MemorySnapshot(**base)


def test_initial_step_emits_no_events():
    d = TransitionDetector()
    events = d.step(_snap(), timestamp_ms=0)
    assert events == []


def test_level_entrance_on_level_start_edge():
    d = TransitionDetector()
    d.step(_snap(level_num=5), timestamp_ms=0)  # prev seeded
    events = d.step(_snap(level_num=5, level_start=1), timestamp_ms=16)

    assert len(events) == 1
    assert isinstance(events[0], LevelEntranceEvent)
    assert events[0].level == 5


def test_no_phantom_checkpoint_on_level_entrance_with_cp_entrance_shift():
    """Backlog E: at a fresh level entry the cp_entrance byte shifts to the
    entry room number. The detector records first_room on the levelNum shift and
    excludes that shift (kaizosplits' !ShiftTo(cpEntrance, firstRoom)), so the
    entrance frame must NOT emit a phantom CheckpointEvent (→ a spurious second
    save state) — only the LevelEntranceEvent."""
    d = TransitionDetector()
    # prev: a different level (3), not on the level-start splash.
    d.step(_snap(level_num=3, room_num=3, level_start=0, cp_entrance=10), timestamp_ms=0)
    # curr: fresh entry to level 5, entry room 20 — level_start 0->1 and
    # cp_entrance shifts to the entry room (20), exactly as the game does.
    events = d.step(
        _snap(level_num=5, room_num=20, level_start=1, cp_entrance=20), timestamp_ms=16
    )

    assert any(isinstance(e, LevelEntranceEvent) for e in events)
    assert not any(isinstance(e, CheckpointEvent) for e in events), \
        "no phantom checkpoint may fire on the level-entrance frame"
    assert len(events) == 1


def test_no_phantom_checkpoint_when_level_num_is_stale_at_entry():
    """Backlog E residual (2026-06-05, "L3 still doubles"): on a level->level
    transition $13BF (level_num) is STALE at the entry frame — it still reads the
    PREVIOUS level and only catches up a frame or more later (confirmed: live
    entrance events report the prior level number). So arming first_room on a
    level_num shift misses the entry frame, and the cp_entrance shift to the
    entry room fires a phantom CheckpointEvent. first_room must instead be armed
    on the level-start entry edge (level_start 0->1), which is the signal that
    actually fires on the entry frame. See feedback_smw_memory_timing."""
    d = TransitionDetector()
    # prev: mid-play in the previous level (3); level_num already settled there.
    d.step(_snap(level_num=3, room_num=3, level_start=0, cp_entrance=10), timestamp_ms=0)
    # entry frame: level_start 0->1 and cp_entrance shifts to the entry room (20),
    # room_num=20 — but level_num is STILL 3 (stale; it lags the real entry).
    events = d.step(
        _snap(level_num=3, room_num=20, level_start=1, cp_entrance=20), timestamp_ms=16
    )

    assert any(isinstance(e, LevelEntranceEvent) for e in events)
    assert not any(isinstance(e, CheckpointEvent) for e in events), \
        "stale level_num must not let a phantom checkpoint through on the entry frame"
    assert len(events) == 1


def test_death_emits_once_per_anim_transition():
    d = TransitionDetector()
    d.step(_snap(player_anim=0), timestamp_ms=0)
    e1 = d.step(_snap(player_anim=9), timestamp_ms=16)
    e2 = d.step(_snap(player_anim=9), timestamp_ms=32)

    assert any(isinstance(e, DeathEvent) for e in e1)
    assert not any(isinstance(e, DeathEvent) for e in e2), "death must not refire while still dying"


def test_exit_emits_on_exit_mode_edge():
    d = TransitionDetector()
    d.step(_snap(exit_mode=0, fanfare=1, level_num=5), timestamp_ms=0)
    events = d.step(_snap(exit_mode=1, fanfare=1, level_num=5), timestamp_ms=16)

    assert any(isinstance(e, LevelExitEvent) and e.goal == "normal" for e in events)


def test_checkpoint_then_spawn_after_death():
    """Real sequence: hit midway -> die -> respawn."""
    d = TransitionDetector()
    # Frame 1: clean snapshot.
    d.step(_snap(level_num=5, midway=0, level_start=1), timestamp_ms=0)
    # Frame 2: midway tape.
    cp_events = d.step(_snap(level_num=5, midway=1, level_start=1), timestamp_ms=16)
    assert any(isinstance(e, CheckpointEvent) and e.cp_type == "midway" for e in cp_events)
    # Frame 3: death.
    d.step(_snap(level_num=5, midway=1, player_anim=9, level_start=1), timestamp_ms=32)
    # Frame 4: still dying.
    d.step(_snap(level_num=5, midway=1, player_anim=9, level_start=0), timestamp_ms=48)
    # Frame 5: respawn — level_start 0 -> 1 with died_flag still set.
    spawn_events = d.step(_snap(level_num=5, midway=1, level_start=1), timestamp_ms=64)
    assert any(isinstance(e, SpawnEvent) and e.is_cold_cp for e in spawn_events)


def test_resync_after_state_load_clears_died_flag():
    """Regression: practice mode reloads state on death; the next death must
    still fire. Before the fix, died_flag stuck True across the resync and
    suppressed all subsequent Death events forever."""
    d = TransitionDetector()
    # Step into PLAYING.
    d.step(_snap(level_num=5, level_start=1), timestamp_ms=0)
    # Player dies — Death fires, died_flag=True.
    e1 = d.step(_snap(level_num=5, level_start=1, player_anim=9), timestamp_ms=16)
    assert any(isinstance(e, DeathEvent) for e in e1)

    # Practice loop reloads the state. resync replaces prev with the loaded
    # snapshot — and (post-fix) clears died_flag.
    d.resync_after_state_load(_snap(level_num=5, level_start=1, player_anim=0))

    # Player dies again. Death MUST fire — died_flag must have been cleared.
    e2 = d.step(_snap(level_num=5, level_start=1, player_anim=9), timestamp_ms=32)
    assert any(isinstance(e, DeathEvent) for e in e2), \
        "second death after state-load was suppressed (died_flag stuck)"


def test_resync_after_state_load_clears_cp_acquired_and_exit_flag():
    """Same pattern — cp_acquired and exit_this_frame must reset, otherwise
    cold-fill sees stale flags and a level_exit on the load-frame can suppress
    a fresh entrance."""
    d = TransitionDetector()
    # Step into PLAYING with a checkpoint hit so cp_acquired and cp_ordinal advance.
    d.step(_snap(level_num=5, midway=0, level_start=1), timestamp_ms=0)
    d.step(_snap(level_num=5, midway=1, level_start=1), timestamp_ms=16)
    assert d._cp_acquired is True
    assert d._state.cp_ordinal == 1

    d.resync_after_state_load(_snap(level_num=5, level_start=1))
    assert d._cp_acquired is False
    assert d._state.cp_ordinal == 0
    assert d._exit_this_frame is False


def test_exit_this_frame_does_not_bleed_to_next_frame():
    """A LevelExit one frame must not suppress LevelEntrance the next frame."""
    d = TransitionDetector()
    d.step(_snap(exit_mode=0, level_num=5), timestamp_ms=0)
    exit_events = d.step(_snap(exit_mode=1, level_num=5), timestamp_ms=16)
    assert any(isinstance(e, LevelExitEvent) for e in exit_events)
    # Next frame: exit_mode still 1 (no edge), level_start 0->1 — entrance must fire.
    entrance_events = d.step(
        _snap(exit_mode=1, level_num=5, level_start=1), timestamp_ms=32
    )
    assert any(isinstance(e, LevelEntranceEvent) for e in entrance_events)


def test_mark_replay_entrance_fires_without_rising_edge():
    """Replay start: prev.level_start can already be 1 (RA paused on title
    demo's level frame), but the detector must still emit a LevelEntranceEvent
    on the first post-replay frame where level_start=1. Without this hook,
    `edge_spawn` requires prev=0, so the entrance is missed forever in the
    one-level Love Yourself replay fixture.
    """
    d = TransitionDetector()
    # Pre-replay: detector sees title-demo state with level_start already 1.
    d.step(_snap(level_num=3, level_start=1), timestamp_ms=0)
    d.step(_snap(level_num=3, level_start=1), timestamp_ms=16)
    # Sanity: a normal step with prev=1, curr=1 fires no entrance.
    normal = d.step(_snap(level_num=3, level_start=1), timestamp_ms=32)
    assert not any(isinstance(e, LevelEntranceEvent) for e in normal)

    d.mark_replay_entrance()
    # PLAY_REPLAY loads the replay's savestate and bumps state_version, so the
    # poller resyncs; that arms the pending replay entrance.
    d.resync_after_state_load(_snap(level_num=5, level_start=1))
    # Replay loaded: level_start is still 1 (replay's savestate is at the splash).
    forced = d.step(_snap(level_num=5, level_start=1), timestamp_ms=48)
    entrance = [e for e in forced if isinstance(e, LevelEntranceEvent)]
    assert len(entrance) == 1
    assert entrance[0].level == 5


def test_mark_replay_entrance_fires_regardless_of_level_start_after_resync():
    """Robustness fix: the replay-start entrance fires even if the poller never
    samples a frame where level_start==1.

    SMW holds level_start=1 for only a few frames at the entrance splash, then
    drops to 0 forever in a one-level replay. Under fast-forward (or a
    shared-NCI-socket read stall during play_movie's verify) the poller can step
    right over that brief window. The OLD logic gated the synthesized entrance on
    `curr.level_start == ACTIVE`, so a missed window meant a missed entrance
    forever (sections_captured stuck at 0 → the replay-fixture 120s hang). The
    state_version bump on PLAY_REPLAY arms the entrance at resync; it must then
    fire on the next step regardless of level_start.
    """
    d = TransitionDetector()
    # Pre-replay: frozen title-demo frame.
    d.step(_snap(level_num=3, level_start=0), timestamp_ms=0)
    d.mark_replay_entrance()
    # Resync snapshot already shows level_start=0 — the splash window was missed.
    d.resync_after_state_load(_snap(level_num=44, level_start=0))
    forced = d.step(_snap(level_num=44, level_start=0), timestamp_ms=32)
    entrance = [e for e in forced if isinstance(e, LevelEntranceEvent)]
    assert len(entrance) == 1
    assert entrance[0].level == 44


def test_mark_replay_entrance_does_not_fire_before_resync():
    """The pending replay entrance must NOT fire until the state-load resync
    arms it. A poller tick landing between mark_replay_entrance() and
    PLAY_REPLAY's savestate load would otherwise synthesize an entrance on the
    stale pre-load (title-demo) frame, with the wrong level_num.
    """
    d = TransitionDetector()
    # prev=1, curr=1: no natural rising edge, so only the forced path could fire.
    d.step(_snap(level_num=3, level_start=1), timestamp_ms=0)
    d.mark_replay_entrance()
    pre = d.step(_snap(level_num=3, level_start=1), timestamp_ms=16)
    assert not any(isinstance(e, LevelEntranceEvent) for e in pre)


def test_mark_replay_entrance_clears_after_firing():
    """The armed entrance is one-shot: once a LevelEntranceEvent is
    synthesized, subsequent frames don't keep refiring."""
    d = TransitionDetector()
    d.step(_snap(level_start=1), timestamp_ms=0)
    d.mark_replay_entrance()
    d.resync_after_state_load(_snap(level_num=5, level_start=1))
    first = d.step(_snap(level_num=5, level_start=1), timestamp_ms=16)
    assert any(isinstance(e, LevelEntranceEvent) for e in first)

    second = d.step(_snap(level_num=5, level_start=1), timestamp_ms=32)
    assert not any(isinstance(e, LevelEntranceEvent) for e in second)


def test_mark_replay_entrance_ignores_stale_died_flag():
    """If pre-replay frames triggered died_flag, the synthesized entrance must
    still be a LevelEntranceEvent (fresh entry), not a SpawnEvent (respawn)."""
    d = TransitionDetector()
    # Pre-replay: simulate a death.
    d.step(_snap(player_anim=0), timestamp_ms=0)
    d.step(_snap(player_anim=9), timestamp_ms=16)
    assert d._state.died_flag is True

    d.mark_replay_entrance()
    d.resync_after_state_load(_snap(level_num=5, level_start=0, player_anim=0))
    forced = d.step(_snap(level_num=5, level_start=0, player_anim=0), timestamp_ms=32)
    assert any(isinstance(e, LevelEntranceEvent) for e in forced)
    assert not any(isinstance(e, SpawnEvent) for e in forced)
    assert d._state.died_flag is False


# ---------------------------------------------------------------------------
# Early finish detection (detect_finish edge → LevelExitEvent before exit_mode)
# ---------------------------------------------------------------------------

def test_goal_tape_fires_level_exit_at_fanfare_edge_not_exit_mode():
    """LevelExitEvent fires when fanfare goes 0→1 (goal tape hit), not when
    exit_mode later goes non-zero.

    Regression for Bug #3: practice timer was stopping at the exit animation
    (~1–2s after actual goal) because the detector only emitted on exit_mode.
    """
    d = TransitionDetector()
    d.step(_snap(level_num=5, fanfare=0, exit_mode=0), timestamp_ms=0)
    # Fanfare fires (goal tape hit) — exit_mode still 0.
    events = d.step(_snap(level_num=5, fanfare=1, exit_mode=0), timestamp_ms=16)
    exits = [e for e in events if isinstance(e, LevelExitEvent)]
    assert len(exits) == 1, f"expected 1 LevelExitEvent at fanfare edge, got {exits}"
    assert exits[0].goal == "normal"


def test_boss_defeat_fires_level_exit_without_exit_mode():
    """Boss defeat (fanfare→1 + boss_defeat non-zero) emits LevelExitEvent(goal='boss')
    even if exit_mode never fires.

    Regression for Bugs #2/#5: game-ending boss defeats (Bowser) play credits
    instead of a normal level-exit sequence, so exit_mode stays 0 forever.
    Practice sessions hung indefinitely because AttemptResultEvent was never
    delivered.
    """
    d = TransitionDetector()
    d.step(_snap(level_num=5, fanfare=0, boss_defeat=1, exit_mode=0), timestamp_ms=0)
    events = d.step(_snap(level_num=5, fanfare=1, boss_defeat=1, exit_mode=0), timestamp_ms=16)
    exits = [e for e in events if isinstance(e, LevelExitEvent)]
    assert len(exits) == 1, f"expected 1 LevelExitEvent for boss defeat, got {exits}"
    assert exits[0].goal == "boss"


def test_early_finish_suppresses_duplicate_when_exit_mode_fires():
    """After detect_finish emits LevelExitEvent, the later exit_mode edge must
    NOT emit a second LevelExitEvent.
    """
    d = TransitionDetector()
    d.step(_snap(level_num=5, fanfare=0, exit_mode=0), timestamp_ms=0)
    # Goal tape fires.
    early = d.step(_snap(level_num=5, fanfare=1, exit_mode=0), timestamp_ms=16)
    assert sum(1 for e in early if isinstance(e, LevelExitEvent)) == 1
    # exit_mode fires a few frames later — must NOT emit a duplicate.
    late = d.step(_snap(level_num=5, fanfare=1, exit_mode=1), timestamp_ms=32)
    assert not any(isinstance(e, LevelExitEvent) for e in late), (
        "duplicate LevelExitEvent from exit_mode after detect_finish already fired"
    )


def test_abort_via_exit_mode_still_fires_when_no_fanfare():
    """Aborts (death-exit, start+select reset) have no fanfare, so detect_finish
    returns None and the exit_mode edge must still emit LevelExitEvent(goal='abort').
    """
    d = TransitionDetector()
    d.step(_snap(level_num=5, fanfare=0, exit_mode=0), timestamp_ms=0)
    events = d.step(_snap(level_num=5, fanfare=0, exit_mode=1), timestamp_ms=16)
    exits = [e for e in events if isinstance(e, LevelExitEvent)]
    assert len(exits) == 1
    assert exits[0].goal == "abort"


def test_finish_emitted_resets_on_next_level_entrance():
    """After a goal fires LevelExitEvent, _finish_emitted resets when the next
    level's entrance is detected, so the second level can also get its exit event.
    """
    d = TransitionDetector()
    # Level 1: goal tape fires.
    d.step(_snap(level_num=1, fanfare=0), timestamp_ms=0)
    d.step(_snap(level_num=1, fanfare=1), timestamp_ms=16)
    assert d._finish_emitted is True

    # Transition to level 2 (edge_spawn: level_start 0→1).
    d.step(_snap(level_num=2, level_start=0, fanfare=0), timestamp_ms=32)
    d.step(_snap(level_num=2, level_start=1, fanfare=0), timestamp_ms=48)
    assert d._finish_emitted is False, "_finish_emitted should reset on new entrance"

    # Level 2 can also detect its goal.
    d.step(_snap(level_num=2, fanfare=0, exit_mode=0), timestamp_ms=64)
    events = d.step(_snap(level_num=2, fanfare=1, exit_mode=0), timestamp_ms=80)
    exits = [e for e in events if isinstance(e, LevelExitEvent)]
    assert len(exits) == 1
