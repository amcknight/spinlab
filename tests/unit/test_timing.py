"""Tests for PracticeTiming and HyperPlayTiming state machines."""
from spinlab.protocol import (
    AttemptResultEvent,
    CheckpointEvent,
    DeathEvent,
    EventAttemptEmission,
    LevelExitEvent,
    HyperPlayCheckpointEvent,
    HyperPlayCompleteEvent,
    HyperPlayDeathEvent,
)
from spinlab.timing import PracticeTiming, HyperPlayEmittedEvent, HyperPlayTiming


class _Clock:
    def __init__(self) -> None:
        self.now = 0

    def __call__(self) -> int:
        return self.now


# -- PracticeTiming -------------------------------------------------------

def test_practice_unarmed_observes_do_nothing():
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming()
    pt.observe_event(DeathEvent())
    pt.tick(now_ms=1000)
    assert received == []
    assert pt.is_armed is False


def test_practice_goal_zero_deaths_emits_completed():
    clock = _Clock()
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    clock.now = 100
    pt.arm(
        segment_id="seg-1",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=1000,
        on_attempt_result=received.append,
    )
    # Player plays for 5000ms and reaches the goal.
    clock.now = 5100  # 5000ms after arm
    pt.observe_event(LevelExitEvent(level=5, goal="normal"))
    # In RESULT now. Tick before auto_advance — no emit.
    clock.now = 5500
    assert pt.tick() is False
    assert received == []
    # After auto_advance — emit.
    clock.now = 6100  # 1000ms in result phase
    assert pt.tick() is True
    assert len(received) == 1
    r = received[0]
    assert isinstance(r, AttemptResultEvent)
    assert r.segment_id == "seg-1"
    assert r.completed is True
    assert r.time_ms == 5000  # elapsed_ms minus 0 deaths
    assert r.deaths == 0
    assert r.clean_tail_ms == 5000  # zero deaths → equals time_ms
    # And the timing is now disarmed.
    assert pt.is_armed is False


def test_practice_goal_with_deaths_applies_penalty():
    clock = _Clock()
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    clock.now = 0
    pt.arm(
        segment_id="seg-2",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=500,
        on_attempt_result=received.append,
    )
    clock.now = 1000
    pt.observe_event(DeathEvent())
    clock.now = 5000
    pt.observe_event(DeathEvent())
    clock.now = 8000
    pt.observe_event(LevelExitEvent(level=5, goal="normal"))
    # elapsed = (8000 - 0) + (3200 * 2) = 8000 + 6400 = 14400
    clock.now = 8500
    assert pt.tick() is True
    r = received[0]
    assert r.completed is True
    assert r.time_ms == 14400
    assert r.deaths == 2
    # clean_tail_ms = floor(result_start_ms - last_death_ms) = 8000 - 5000 = 3000
    assert r.clean_tail_ms == 3000


def test_practice_abort_exit_marks_failed():
    """level_exit with goal=='abort' marks the attempt failed."""
    clock = _Clock()
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    clock.now = 0
    pt.arm(
        segment_id="seg-3",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=100,
        on_attempt_result=received.append,
    )
    clock.now = 2000
    pt.observe_event(LevelExitEvent(level=5, goal="abort"))
    clock.now = 2200
    assert pt.tick() is True
    r = received[0]
    assert r.completed is False
    assert r.time_ms == 2000  # no deaths penalty
    assert r.clean_tail_ms is None


def test_practice_checkpoint_endtype_completes_on_checkpoint_event():
    clock = _Clock()
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    clock.now = 0
    pt.arm(
        segment_id="seg-cp",
        end_type="checkpoint",
        death_penalty_ms=3200,
        auto_advance_delay_ms=100,
        on_attempt_result=received.append,
    )
    clock.now = 3000
    pt.observe_event(CheckpointEvent(level_num=5, cp_ordinal=1))
    clock.now = 3200
    assert pt.tick() is True
    assert received[0].completed is True
    assert received[0].time_ms == 3000


def test_practice_checkpoint_endtype_ignores_level_exit():
    """When end_type is checkpoint, level_exit doesn't end the attempt."""
    clock = _Clock()
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    pt.arm(
        segment_id="seg-cp",
        end_type="checkpoint",
        death_penalty_ms=3200,
        auto_advance_delay_ms=100,
        on_attempt_result=received.append,
    )
    pt.observe_event(LevelExitEvent(level=5, goal="normal"))
    pt.tick(now_ms=10000)
    assert received == []  # level_exit ignored when waiting for a checkpoint
    assert pt.is_armed is True


def test_practice_disarm_emits_nothing():
    clock = _Clock()
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    pt.arm(
        segment_id="seg-x",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=100,
        on_attempt_result=received.append,
    )
    pt.observe_event(DeathEvent())
    pt.disarm()
    pt.tick(now_ms=99999)
    assert received == []
    assert pt.is_armed is False


def test_practice_re_arm_clears_previous_state():
    clock = _Clock()
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    pt.arm(
        segment_id="seg-1",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=100,
        on_attempt_result=received.append,
    )
    pt.observe_event(DeathEvent())
    # Re-arm without disarming
    clock.now = 1000
    pt.arm(
        segment_id="seg-2",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=100,
        on_attempt_result=received.append,
    )
    clock.now = 3000
    pt.observe_event(LevelExitEvent(level=5, goal="normal"))
    clock.now = 3200
    assert pt.tick() is True
    r = received[0]
    assert r.segment_id == "seg-2"
    assert r.deaths == 0  # death from prior arm cleared
    assert r.time_ms == 2000  # 3000 - 1000


# -- HyperPlayTiming -------------------------------------------------------

def test_hyper_play_basic_smoke():
    """Smoke: arming and disarm work."""
    clock = _Clock()
    sr = HyperPlayTiming(now_ms=clock)
    sr.arm(
        segment_id="run-1",
        checkpoints=[],
        on_event=lambda _: None,
    )
    assert sr.is_armed is True
    sr.disarm()
    assert sr.is_armed is False


def test_hyper_play_arm_stores_delay_kwargs():
    """arm() captures death_delay_ms and auto_advance_delay_ms for later use."""
    sr = HyperPlayTiming()
    sr.arm(
        segment_id="run-1",
        checkpoints=[],
        auto_advance_delay_ms=2500,
        death_delay_ms=2000,
        on_event=lambda d: None,
    )
    # Smoke check: kwargs accepted; instance has the values.
    assert sr._auto_advance_delay_ms == 2500
    assert sr._death_delay_ms == 2000


def _make_hyper_play(clock: _Clock, received: list, **kwargs) -> HyperPlayTiming:
    sr = HyperPlayTiming(now_ms=clock)
    sr.arm(
        segment_id="run-1",
        checkpoints=kwargs.pop("checkpoints", [{"ordinal": 1}, {"ordinal": 2}]),
        on_event=received.append,
        death_delay_ms=kwargs.pop("death_delay_ms", 1500),
        auto_advance_delay_ms=kwargs.pop("auto_advance_delay_ms", 1000),
    )
    return sr


def test_hyper_play_death_emits_event_and_enters_dying():
    clock = _Clock()
    received: list[HyperPlayEmittedEvent] = []
    sr = _make_hyper_play(clock, received)

    clock.now = 2500
    sr.observe_event(DeathEvent())

    assert len(received) == 1
    ev = received[0]
    assert isinstance(ev, HyperPlayDeathEvent)
    assert ev.elapsed_ms == 2500
    assert ev.split_ms == 2500
    # State is DYING — observe_event no-ops for further events while in DYING.
    sr.observe_event(DeathEvent())
    assert len(received) == 1


def test_hyper_play_checkpoint_emits_event_and_advances_index():
    clock = _Clock()
    received: list[HyperPlayEmittedEvent] = []
    sr = _make_hyper_play(clock, received)

    clock.now = 1500
    sr.observe_event(CheckpointEvent(level_num=5, cp_ordinal=99))

    # Note: ordinal comes from the checkpoints list arm() was given, not the event.
    assert len(received) == 1
    ev = received[0]
    assert isinstance(ev, HyperPlayCheckpointEvent)
    assert ev.ordinal == 1  # first checkpoint in the list
    assert ev.elapsed_ms == 1500
    assert ev.split_ms == 1500

    # Second checkpoint advances the index.
    clock.now = 3000
    sr.observe_event(CheckpointEvent(level_num=5, cp_ordinal=99))
    assert len(received) == 2
    assert received[1].ordinal == 2


def test_hyper_play_checkpoint_past_end_of_list_no_emit():
    """checkpoints exhausted — extra checkpoint events are silently dropped."""
    clock = _Clock()
    received: list[HyperPlayEmittedEvent] = []
    sr = _make_hyper_play(clock, received, checkpoints=[{"ordinal": 1}])

    sr.observe_event(CheckpointEvent(level_num=5, cp_ordinal=99))
    sr.observe_event(CheckpointEvent(level_num=5, cp_ordinal=99))

    assert len(received) == 1, "second checkpoint should not emit"


def test_hyper_play_checkpoint_non_dict_uses_positional_ordinal():
    """checkpoints can be opaque objects; if not a dict with 'ordinal',
    HyperPlayTiming falls back to the 1-based index."""
    clock = _Clock()
    received: list[HyperPlayEmittedEvent] = []
    sr = _make_hyper_play(clock, received, checkpoints=["unused-string", "another"])

    sr.observe_event(CheckpointEvent(level_num=5, cp_ordinal=99))
    sr.observe_event(CheckpointEvent(level_num=5, cp_ordinal=99))

    assert received[0].ordinal == 1
    assert received[1].ordinal == 2


def test_hyper_play_level_exit_enters_result_state():
    """A non-abort LevelExit transitions to RESULT; no event emitted yet."""
    clock = _Clock()
    received: list[HyperPlayEmittedEvent] = []
    sr = _make_hyper_play(clock, received)

    clock.now = 4000
    sr.observe_event(LevelExitEvent(level=5, goal="normal"))

    # No event emitted at the moment of exit; complete event waits for the
    # auto_advance window to elapse via tick().
    assert received == []
    assert sr.is_armed is True  # still in RESULT, not idle yet


def test_hyper_play_level_exit_abort_dispatches_death():
    """abort exit (pit-fall in SMW) is treated as a death — emits
    HyperPlayDeathEvent and enters DYING so the run loop reloads cold state."""
    clock = _Clock()
    received: list[HyperPlayEmittedEvent] = []
    sr = _make_hyper_play(clock, received)

    clock.now = 1234
    sr.observe_event(LevelExitEvent(level=5, goal="abort"))

    assert len(received) == 1
    assert isinstance(received[0], HyperPlayDeathEvent)
    # In DYING; subsequent events are ignored until tick() resumes PLAYING.
    sr.observe_event(CheckpointEvent(level_num=5, cp_ordinal=99))
    assert len(received) == 1


def test_hyper_play_tick_in_dying_resumes_playing_after_delay():
    clock = _Clock()
    received: list[HyperPlayEmittedEvent] = []
    sr = _make_hyper_play(clock, received, death_delay_ms=1500)

    clock.now = 2000
    sr.observe_event(DeathEvent())  # enters DYING
    # Tick before delay elapses — stays in DYING.
    clock.now = 2500
    assert sr.tick() is False
    # State is still DYING; observing a checkpoint would no-op.
    sr.observe_event(CheckpointEvent(level_num=5, cp_ordinal=99))
    assert len(received) == 1  # only the DeathEvent
    # Tick after the delay — transitions back to PLAYING.
    clock.now = 3500  # 1500ms after death
    assert sr.tick() is False  # no result event from DYING → PLAYING transition
    # Now in PLAYING — a checkpoint event emits.
    sr.observe_event(CheckpointEvent(level_num=5, cp_ordinal=99))
    assert len(received) == 2


def test_hyper_play_dying_blackout_excludes_dead_time_from_elapsed():
    """The DYING blackout duration is bumped into start_ms so it doesn't
    count toward elapsed time after the player respawns."""
    clock = _Clock()
    received: list[HyperPlayEmittedEvent] = []
    sr = _make_hyper_play(clock, received, death_delay_ms=1500)

    clock.now = 1000
    sr.observe_event(DeathEvent())  # elapsed at this point = 1000ms

    # Tick past blackout.
    clock.now = 2600  # 1600ms after death — past 1500 threshold
    sr.tick()

    # Now in PLAYING. Observe a checkpoint — split_ms should be 0 since
    # split_ms was reset at end of blackout (i.e. clock.now=2600).
    clock.now = 3100
    sr.observe_event(CheckpointEvent(level_num=5, cp_ordinal=99))
    cp_ev = received[-1]
    assert isinstance(cp_ev, HyperPlayCheckpointEvent)
    # Elapsed accounts for the bumped start_ms (start_ms += blackout_duration).
    # elapsed = now - start_ms = 3100 - (0 + 1600) = 1500
    assert cp_ev.elapsed_ms == 1500


def test_hyper_play_tick_in_result_emits_complete_event_after_delay():
    clock = _Clock()
    received: list[HyperPlayEmittedEvent] = []
    sr = _make_hyper_play(clock, received, auto_advance_delay_ms=1000)

    clock.now = 4000
    sr.observe_event(LevelExitEvent(level=5, goal="normal"))  # enters RESULT
    # Tick before auto_advance — no emit.
    clock.now = 4500
    assert sr.tick() is False
    assert received == []
    # Tick after auto_advance — emit + return to IDLE.
    clock.now = 5100
    assert sr.tick() is True
    assert len(received) == 1
    ev = received[0]
    assert isinstance(ev, HyperPlayCompleteEvent)
    assert ev.elapsed_ms == 4000
    assert ev.split_ms == 4000
    assert sr.is_armed is False


def test_hyper_play_unarmed_observes_do_nothing():
    """observe_event before arm() is a no-op."""
    received: list[HyperPlayEmittedEvent] = []
    sr = HyperPlayTiming()
    sr.observe_event(DeathEvent())
    sr.tick(now_ms=1000)
    assert received == []
    assert sr.is_armed is False


# -- PracticeTiming per-event emission (Phase 0) --------------------------

def test_practice_emits_event_attempts_for_two_deaths_then_clear():
    """Phase 0 test 2: feed 2 deaths + 1 clear; expect 3 EventAttemptEmissions
    with the right outcomes, deltas, and shared episode_id."""
    clock = _Clock()
    received_events: list[EventAttemptEmission] = []
    received_results: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    clock.now = 100
    pt.arm(
        segment_id="seg-2deaths",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=500,
        on_attempt_result=received_results.append,
        on_event_attempt=received_events.append,
    )
    # First death at +1500ms after arm.
    clock.now = 1600
    pt.observe_event(DeathEvent())
    # Second death at +2500ms after the first.
    clock.now = 4100
    pt.observe_event(DeathEvent())
    # Survived at +3000ms after the second death.
    clock.now = 7100
    pt.observe_event(LevelExitEvent(level=5, goal="normal"))

    assert len(received_events) == 3
    assert [e.outcome for e in received_events] == ["died", "died", "survived"]
    assert [e.time_ms for e in received_events] == [1500, 2500, 3000]
    # Shared episode_id across the three events of one armed attempt.
    assert len({e.episode_id for e in received_events}) == 1
    assert all(e.segment_id == "seg-2deaths" for e in received_events)
    # First event's time_ms is the delta from arm() (start_ms), not 0.
    assert received_events[0].timestamp_ms == 1600


def test_practice_no_event_attempt_callback_is_fine():
    """The new callback is optional — existing call sites that only pass
    on_attempt_result keep working without modification."""
    clock = _Clock()
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    clock.now = 0
    pt.arm(
        segment_id="seg",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=100,
        on_attempt_result=received.append,
    )
    clock.now = 5000
    pt.observe_event(DeathEvent())
    clock.now = 8000
    pt.observe_event(LevelExitEvent(level=1, goal="normal"))
    clock.now = 8200
    pt.tick()
    assert len(received) == 1


def test_practice_abort_emits_no_survived_event():
    """LevelExit(abort) closes the episode without a 'survived' row — the
    legacy adapter rolls up the death-only events into completed=False."""
    clock = _Clock()
    events: list[EventAttemptEmission] = []
    pt = PracticeTiming(now_ms=clock)
    clock.now = 0
    pt.arm(
        segment_id="seg-abort",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=100,
        on_attempt_result=lambda _: None,
        on_event_attempt=events.append,
    )
    clock.now = 2000
    pt.observe_event(DeathEvent())
    clock.now = 5000
    pt.observe_event(LevelExitEvent(level=1, goal="abort"))
    # Only the death event should have been emitted.
    assert len(events) == 1
    assert events[0].outcome == "died"


def test_practice_checkpoint_endtype_emits_survived_on_checkpoint():
    """End-type 'checkpoint' fires a 'survived' EventAttemptEmission on a
    CheckpointEvent (not LevelExit)."""
    clock = _Clock()
    events: list[EventAttemptEmission] = []
    pt = PracticeTiming(now_ms=clock)
    clock.now = 0
    pt.arm(
        segment_id="seg-cp",
        end_type="checkpoint",
        death_penalty_ms=3200,
        auto_advance_delay_ms=100,
        on_attempt_result=lambda _: None,
        on_event_attempt=events.append,
    )
    clock.now = 4500
    pt.observe_event(CheckpointEvent(level_num=1, cp_ordinal=1))
    assert len(events) == 1
    assert events[0].outcome == "survived"
    assert events[0].time_ms == 4500


def test_practice_new_arm_mints_new_episode_id():
    """Each arm() starts a fresh episode — no leakage of episode_id across
    attempts. (PracticeSession reuses one PracticeTiming across arms.)"""
    clock = _Clock()
    events: list[EventAttemptEmission] = []
    pt = PracticeTiming(now_ms=clock)
    pt.arm(
        segment_id="s", end_type="goal", death_penalty_ms=3200,
        auto_advance_delay_ms=10,
        on_attempt_result=lambda _: None, on_event_attempt=events.append,
    )
    clock.now = 1000
    pt.observe_event(LevelExitEvent(level=1, goal="normal"))
    clock.now = 1100
    pt.tick()
    pt.arm(
        segment_id="s", end_type="goal", death_penalty_ms=3200,
        auto_advance_delay_ms=10,
        on_attempt_result=lambda _: None, on_event_attempt=events.append,
    )
    clock.now = 2500
    pt.observe_event(LevelExitEvent(level=1, goal="normal"))
    assert len(events) == 2
    assert events[0].episode_id != events[1].episode_id


def test_practice_boss_defeat_completes_attempt():
    """LevelExitEvent(goal='boss') must complete a goal-type attempt.

    Regression for Bugs #2/#5: before the detect_finish fix, game-ending boss
    defeats never produced a LevelExitEvent (exit_mode never fired), so the
    practice session hung indefinitely. This test documents that timing already
    handles goal='boss' correctly — the fix lives in the detector.
    """
    clock = _Clock()
    received: list[AttemptResultEvent] = []
    pt = PracticeTiming(now_ms=clock)
    clock.now = 0
    pt.arm(
        segment_id="boss-seg",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=100,
        on_attempt_result=received.append,
    )
    clock.now = 8000
    pt.observe_event(LevelExitEvent(level=49, goal="boss"))
    clock.now = 8200
    assert pt.tick() is True
    assert received[0].completed is True
    assert received[0].time_ms == 8000
