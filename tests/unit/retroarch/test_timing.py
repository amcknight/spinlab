"""Tests for PracticeTiming and SpeedRunTiming state machines."""
from spinlab.protocol import (
    AttemptResultEvent,
    CheckpointEvent,
    DeathEvent,
    LevelExitEvent,
)
from spinlab.retroarch.timing import PracticeTiming, SpeedRunTiming


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


# -- SpeedRunTiming -------------------------------------------------------

def test_speed_run_basic_smoke():
    """Smoke: arming and disarm work."""
    clock = _Clock()
    sr = SpeedRunTiming(now_ms=clock)
    sr.arm(
        segment_id="run-1",
        checkpoints=[],
        on_event=lambda _: None,
    )
    assert sr.is_armed is True
    sr.disarm()
    assert sr.is_armed is False


def test_speed_run_arm_stores_delay_kwargs():
    """arm() captures death_delay_ms and auto_advance_delay_ms for later use."""
    sr = SpeedRunTiming()
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
