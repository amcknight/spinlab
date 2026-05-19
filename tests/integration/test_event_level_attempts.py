"""Phase 0 test 6 — end-to-end integration test for event-level attempts.

Drives real DeathEvents + LevelExitEvents through the RA harness's poke
engine, feeds them into a PracticeTiming armed with the new
``on_event_attempt`` callback, and asserts the emitted
``EventAttemptEmission`` stream matches the per-event production contract:
each death produces one ``died`` event, the final exit produces one
``survived`` event, and every event in one armed attempt shares an
``episode_id``.

This is the "test 6" entry in the Phase 0 plan
(``docs/superpowers/plans/2026-05-18-segments-v07-phase0-event-level-attempts.md``).
"""
from __future__ import annotations

import asyncio

import pytest

from spinlab.protocol import (
    DeathEvent,
    EventAttemptEmission,
    LevelExitEvent,
)
from spinlab.timing import PracticeTiming

pytestmark = [pytest.mark.emulator, pytest.mark.asyncio(loop_scope="session")]


async def _gather_events(run_scenario, scenario_name: str, timeout: float = 30.0) -> list:
    return await run_scenario(scenario_name, timeout=timeout)


async def test_death_event_from_real_ra_pokes_fires_event_attempt(run_scenario):
    """Sanity: a DeathEvent produced by real RA pokes drives a 'died'
    EventAttemptEmission through PracticeTiming."""
    events = await _gather_events(run_scenario, "entrance_death_spawn.poke")
    deaths = [e for e in events if isinstance(e, DeathEvent)]
    assert deaths, "Expected at least one DeathEvent from entrance_death_spawn scenario"

    emissions: list[EventAttemptEmission] = []
    pt = PracticeTiming()
    pt.arm(
        segment_id="seg-integration",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=1000,
        on_attempt_result=lambda _: None,
        on_event_attempt=emissions.append,
    )
    for ev in deaths:
        pt.observe_event(ev)

    assert len(emissions) == len(deaths)
    assert all(e.outcome == "died" for e in emissions)
    # Shared episode_id within one armed attempt.
    assert len({e.episode_id for e in emissions}) == 1


async def test_survived_event_from_real_ra_pokes_fires_event_attempt(run_scenario):
    """A normal LevelExit fired by real RA pokes produces a 'survived'
    EventAttemptEmission whose time_ms is the wall-clock elapsed since
    arm() (or since the previous event)."""
    events = await _gather_events(run_scenario, "entrance_goal.poke")
    exits = [e for e in events if isinstance(e, LevelExitEvent) and e.goal != "abort"]
    assert exits, "Expected at least one non-abort LevelExitEvent"

    emissions: list[EventAttemptEmission] = []
    pt = PracticeTiming()
    pt.arm(
        segment_id="seg-integration",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=1000,
        on_attempt_result=lambda _: None,
        on_event_attempt=emissions.append,
    )
    for ev in exits:
        pt.observe_event(ev)

    # Only the first non-abort exit closes the episode; subsequent observes
    # while in RESULT are ignored. That's the expected practice semantics.
    assert len(emissions) == 1
    assert emissions[0].outcome == "survived"
    assert emissions[0].time_ms >= 0


async def test_combined_death_then_clear_produces_paired_episode(run_scenario):
    """End-to-end: feed a DeathEvent (from one scenario) and a LevelExit
    (from another) through one PracticeTiming arm() and verify the rolled-up
    EventAttemptEmission stream looks like one episode with one death + one
    survived event, sharing an episode_id."""
    death_events = [
        e for e in await _gather_events(run_scenario, "entrance_death_spawn.poke")
        if isinstance(e, DeathEvent)
    ]
    exit_events = [
        e for e in await _gather_events(run_scenario, "entrance_goal.poke")
        if isinstance(e, LevelExitEvent) and e.goal != "abort"
    ]
    assert death_events and exit_events

    emissions: list[EventAttemptEmission] = []
    pt = PracticeTiming()
    pt.arm(
        segment_id="seg-combined",
        end_type="goal",
        death_penalty_ms=3200,
        auto_advance_delay_ms=10,
        on_attempt_result=lambda _: None,
        on_event_attempt=emissions.append,
    )
    pt.observe_event(death_events[0])
    pt.observe_event(exit_events[0])

    assert len(emissions) == 2
    assert [e.outcome for e in emissions] == ["died", "survived"]
    assert emissions[0].episode_id == emissions[1].episode_id
    # First event delta is measured against arm() time; second against the
    # death. Both are non-negative.
    assert emissions[0].time_ms >= 0
    assert emissions[1].time_ms >= 0
