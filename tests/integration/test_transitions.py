"""Emulator tests: transition detection via memory pokes in headless RA.

Each test function runs one scenario and makes all assertions in one place.
Run with: pytest -m emulator
Skip automatically if RetroArch or ROM not found.
"""
from __future__ import annotations

import pytest

from spinlab.protocol import (
    CheckpointEvent,
    DeathEvent,
    LevelEntranceEvent,
    LevelExitEvent,
    SpawnEvent,
)

pytestmark = [pytest.mark.emulator, pytest.mark.asyncio(loop_scope="session")]


async def test_entrance_goal(run_scenario):
    """Level entrance followed by normal goal exit."""
    events = await run_scenario("entrance_goal.poke")

    entrances = [e for e in events if isinstance(e, LevelEntranceEvent)]
    assert len(entrances) == 1, f"Expected 1 entrance, got {len(entrances)}: {entrances}"
    assert entrances[0].level == 105, f"Expected level=105, got {entrances[0].level}"

    exits = [e for e in events if isinstance(e, LevelExitEvent)]
    assert len(exits) == 1, f"Expected 1 exit, got {len(exits)}: {exits}"
    assert exits[0].goal == "normal", f"Expected goal='normal', got {exits[0].goal!r}"
    assert exits[0].level == 105, f"Expected level=105, got {exits[0].level}"
    assert exits[0].elapsed_ms > 0, f"Expected elapsed_ms > 0, got {exits[0].elapsed_ms}"


async def test_entrance_death_spawn(run_scenario):
    """Enter level, die, respawn."""
    events = await run_scenario("entrance_death_spawn.poke")
    types = [type(e) for e in events]

    assert types.count(DeathEvent) == 1, f"Expected 1 DeathEvent, got {types.count(DeathEvent)}: {types}"
    assert types.count(SpawnEvent) == 1, f"Expected 1 SpawnEvent, got {types.count(SpawnEvent)}: {types}"

    entrance_idx = types.index(LevelEntranceEvent)
    death_idx = types.index(DeathEvent)
    spawn_idx = types.index(SpawnEvent)
    assert entrance_idx < death_idx < spawn_idx, (
        f"Expected entrance<death<spawn, got idx={entrance_idx},{death_idx},{spawn_idx} types={types}"
    )


async def test_checkpoint_cold_spawn(run_scenario):
    """Enter, hit midway, die, cold respawn."""
    events = await run_scenario("checkpoint_cold_spawn.poke")
    types = [type(e) for e in events]

    assert LevelEntranceEvent in types, f"Expected LevelEntranceEvent in events, got {types}"
    assert CheckpointEvent in types, f"Expected CheckpointEvent in events, got {types}"
    assert DeathEvent in types, f"Expected DeathEvent in events, got {types}"
    assert SpawnEvent in types, f"Expected SpawnEvent in events, got {types}"

    cps = [e for e in events if isinstance(e, CheckpointEvent)]
    assert len(cps) == 1, f"Expected 1 checkpoint, got {len(cps)}: {cps}"
    assert cps[0].cp_ordinal == 1, f"Expected cp_ordinal=1, got {cps[0].cp_ordinal}"

    spawns = [e for e in events if isinstance(e, SpawnEvent)]
    assert len(spawns) == 1, f"Expected 1 spawn, got {len(spawns)}: {spawns}"
    assert spawns[0].is_cold_cp is True, f"Expected is_cold_cp=True, got {spawns[0].is_cold_cp}"


async def test_key_exit(run_scenario):
    """Enter level, exit with key."""
    events = await run_scenario("key_exit.poke")
    exits = [e for e in events if isinstance(e, LevelExitEvent)]
    assert len(exits) == 1, f"Expected 1 exit, got {len(exits)}: {exits}"
    assert exits[0].goal == "key", f"Expected goal='key', got {exits[0].goal!r}"


async def test_orb_exit(run_scenario):
    """Enter level, exit with orb."""
    events = await run_scenario("orb_exit.poke")
    exits = [e for e in events if isinstance(e, LevelExitEvent)]
    assert len(exits) == 1, f"Expected 1 exit, got {len(exits)}: {exits}"
    assert exits[0].goal == "orb", f"Expected goal='orb', got {exits[0].goal!r}"


async def test_multiple_checkpoints(run_scenario):
    """Entrance, checkpoint, death, cold respawn, second checkpoint."""
    events = await run_scenario("multiple_checkpoints.poke")

    cps = [e for e in events if isinstance(e, CheckpointEvent)]
    assert len(cps) == 2, f"Expected 2 checkpoints, got {len(cps)}: {cps}"
    assert cps[0].cp_ordinal == 1, f"Expected first cp_ordinal=1, got {cps[0].cp_ordinal}"
    assert cps[1].cp_ordinal == 2, f"Expected second cp_ordinal=2, got {cps[1].cp_ordinal}"

    spawns = [e for e in events if isinstance(e, SpawnEvent)]
    assert len(spawns) == 1, f"Expected 1 spawn, got {len(spawns)}: {spawns}"
    assert spawns[0].is_cold_cp is True, f"Expected is_cold_cp=True, got {spawns[0].is_cold_cp}"


async def test_death_before_checkpoint(run_scenario):
    """Entrance, death, respawn with no checkpoint hit."""
    events = await run_scenario("death_before_checkpoint.poke")

    spawns = [e for e in events if isinstance(e, SpawnEvent)]
    assert len(spawns) == 1, f"Expected 1 spawn, got {len(spawns)}: {spawns}"
    assert spawns[0].is_cold_cp is False, f"Expected is_cold_cp=False, got {spawns[0].is_cold_cp}"

    cps = [e for e in events if isinstance(e, CheckpointEvent)]
    assert len(cps) == 0, f"Expected 0 checkpoints, got {len(cps)}: {cps}"


async def test_boss_defeat(run_scenario):
    """Entrance, boss defeat + fanfare + exit on same frame."""
    events = await run_scenario("boss_defeat.poke")
    exits = [e for e in events if isinstance(e, LevelExitEvent)]
    assert len(exits) == 1, f"Expected 1 exit, got {len(exits)}: {exits}"
    assert exits[0].goal == "boss", f"Expected goal='boss', got {exits[0].goal!r}"
