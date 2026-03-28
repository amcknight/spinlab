"""Integration tests: transition detection via memory pokes in headless Mesen2.

These tests require Mesen2 installed and a test ROM available.
Run with: pytest -m integration
Skip automatically if Mesen2 or ROM not found.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]


class TestEntranceGoal:
    """Level entrance followed by normal goal exit."""

    async def test_level_entrance_event(self, run_scenario):
        events = await run_scenario("entrance_goal.poke")
        entrances = [e for e in events if e["event"] == "level_entrance"]
        assert len(entrances) == 1
        assert entrances[0]["level"] == 105

    async def test_level_exit_event(self, run_scenario):
        events = await run_scenario("entrance_goal.poke")
        exits = [e for e in events if e["event"] == "level_exit"]
        assert len(exits) == 1
        assert exits[0]["goal"] == "normal"
        assert exits[0]["level"] == 105

    async def test_elapsed_time_positive(self, run_scenario):
        events = await run_scenario("entrance_goal.poke")
        exits = [e for e in events if e["event"] == "level_exit"]
        assert len(exits) == 1
        assert exits[0]["elapsed_ms"] > 0


class TestEntranceDeathSpawn:
    """Enter level, die, respawn."""

    async def test_death_event(self, run_scenario):
        events = await run_scenario("entrance_death_spawn.poke")
        deaths = [e for e in events if e["event"] == "death"]
        assert len(deaths) == 1

    async def test_spawn_event(self, run_scenario):
        events = await run_scenario("entrance_death_spawn.poke")
        spawns = [e for e in events if e["event"] == "spawn"]
        assert len(spawns) == 1

    async def test_event_order(self, run_scenario):
        events = await run_scenario("entrance_death_spawn.poke")
        event_names = [e["event"] for e in events]
        entrance_idx = event_names.index("level_entrance")
        death_idx = event_names.index("death")
        spawn_idx = event_names.index("spawn")
        assert entrance_idx < death_idx < spawn_idx


class TestCheckpointColdSpawn:
    """Enter, hit midway, die, cold respawn."""

    async def test_checkpoint_event(self, run_scenario):
        events = await run_scenario("checkpoint_cold_spawn.poke")
        cps = [e for e in events if e["event"] == "checkpoint"]
        assert len(cps) == 1
        assert cps[0]["cp_ordinal"] == 1

    async def test_cold_spawn(self, run_scenario):
        events = await run_scenario("checkpoint_cold_spawn.poke")
        spawns = [e for e in events if e["event"] == "spawn"]
        assert len(spawns) == 1
        assert spawns[0]["is_cold_cp"] is True

    async def test_full_event_sequence(self, run_scenario):
        events = await run_scenario("checkpoint_cold_spawn.poke")
        event_names = [e["event"] for e in events]
        assert "level_entrance" in event_names
        assert "checkpoint" in event_names
        assert "death" in event_names
        assert "spawn" in event_names


class TestKeyExit:
    """Enter level, exit with key."""

    async def test_key_goal_type(self, run_scenario):
        events = await run_scenario("key_exit.poke")
        exits = [e for e in events if e["event"] == "level_exit"]
        assert len(exits) == 1
        assert exits[0]["goal"] == "key"


class TestOrbExit:
    """Enter level, exit with orb."""

    async def test_orb_goal_type(self, run_scenario):
        events = await run_scenario("orb_exit.poke")
        exits = [e for e in events if e["event"] == "level_exit"]
        assert len(exits) == 1
        assert exits[0]["goal"] == "orb"


class TestSameFrameExitEntrance:
    """Exit and entrance on same frame — entrance should be suppressed."""

    async def test_entrance_suppressed(self, run_scenario):
        events = await run_scenario("same_frame_exit_entrance.poke")
        # There should be exactly one entrance (from frame 2) and two exits.
        # The frame-20 entrance (same frame as exit) should be suppressed.
        entrances = [e for e in events if e["event"] == "level_entrance"]
        exits = [e for e in events if e["event"] == "level_exit"]
        assert len(entrances) == 1, f"Expected 1 entrance, got {len(entrances)}: {entrances}"
        assert len(exits) == 2, f"Expected 2 exits, got {len(exits)}: {exits}"
