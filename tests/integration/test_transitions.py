"""Integration tests: transition detection via memory pokes in headless Mesen2.

Each test function runs one scenario and makes all assertions in one place.
Run with: pytest -m integration
Skip automatically if Mesen2 or ROM not found.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def test_entrance_goal(run_scenario):
    """Level entrance followed by normal goal exit."""
    events = await run_scenario("entrance_goal.poke")

    entrances = [e for e in events if e["event"] == "level_entrance"]
    assert len(entrances) == 1
    assert entrances[0]["level"] == 105

    exits = [e for e in events if e["event"] == "level_exit"]
    assert len(exits) == 1
    assert exits[0]["goal"] == "normal"
    assert exits[0]["level"] == 105
    assert exits[0]["elapsed_ms"] > 0


async def test_entrance_death_spawn(run_scenario):
    """Enter level, die, respawn."""
    events = await run_scenario("entrance_death_spawn.poke")
    event_names = [e["event"] for e in events]

    assert event_names.count("death") == 1
    assert event_names.count("spawn") == 1

    entrance_idx = event_names.index("level_entrance")
    death_idx = event_names.index("death")
    spawn_idx = event_names.index("spawn")
    assert entrance_idx < death_idx < spawn_idx


async def test_checkpoint_cold_spawn(run_scenario):
    """Enter, hit midway, die, cold respawn."""
    events = await run_scenario("checkpoint_cold_spawn.poke")
    event_names = [e["event"] for e in events]

    assert "level_entrance" in event_names
    assert "checkpoint" in event_names
    assert "death" in event_names
    assert "spawn" in event_names

    cps = [e for e in events if e["event"] == "checkpoint"]
    assert len(cps) == 1
    assert cps[0]["cp_ordinal"] == 1

    spawns = [e for e in events if e["event"] == "spawn"]
    assert len(spawns) == 1
    assert spawns[0]["is_cold_cp"] is True


async def test_key_exit(run_scenario):
    """Enter level, exit with key."""
    events = await run_scenario("key_exit.poke")
    exits = [e for e in events if e["event"] == "level_exit"]
    assert len(exits) == 1
    assert exits[0]["goal"] == "key"


async def test_orb_exit(run_scenario):
    """Enter level, exit with orb."""
    events = await run_scenario("orb_exit.poke")
    exits = [e for e in events if e["event"] == "level_exit"]
    assert len(exits) == 1
    assert exits[0]["goal"] == "orb"


async def test_multiple_checkpoints(run_scenario):
    """Entrance, checkpoint, death, cold respawn, second checkpoint."""
    events = await run_scenario("multiple_checkpoints.poke")
    event_names = [e["event"] for e in events]

    cps = [e for e in events if e["event"] == "checkpoint"]
    assert len(cps) == 2
    assert cps[0]["cp_ordinal"] == 1
    assert cps[1]["cp_ordinal"] == 2

    spawns = [e for e in events if e["event"] == "spawn"]
    assert len(spawns) == 1
    assert spawns[0]["is_cold_cp"] is True


async def test_death_before_checkpoint(run_scenario):
    """Entrance, death, respawn with no checkpoint hit."""
    events = await run_scenario("death_before_checkpoint.poke")

    spawns = [e for e in events if e["event"] == "spawn"]
    assert len(spawns) == 1
    assert spawns[0]["is_cold_cp"] is False

    cps = [e for e in events if e["event"] == "checkpoint"]
    assert len(cps) == 0


async def test_boss_defeat(run_scenario):
    """Entrance, boss defeat + fanfare + exit on same frame."""
    events = await run_scenario("boss_defeat.poke")
    exits = [e for e in events if e["event"] == "level_exit"]
    assert len(exits) == 1
    assert exits[0]["goal"] == "boss"


async def test_same_frame_exit_entrance(run_scenario):
    """Exit and entrance on same frame — entrance should be suppressed."""
    events = await run_scenario("same_frame_exit_entrance.poke")
    entrances = [e for e in events if e["event"] == "level_entrance"]
    exits = [e for e in events if e["event"] == "level_exit"]
    assert len(entrances) == 1, f"Expected 1 entrance, got {len(entrances)}: {entrances}"
    assert len(exits) == 2, f"Expected 2 exits, got {len(exits)}: {exits}"
