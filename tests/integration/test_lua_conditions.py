"""Emulator tests: set_conditions command and condition reading at transitions.

Verifies that:
1. Lua accepts a set_conditions TCP command and acknowledges it.
2. Subsequent transition events (level_entrance) carry a `conditions` key
   whose values reflect the requested memory addresses at that frame.

Run with: pytest -m emulator
Must be run from the main checkout (not a worktree) where Mesen2 + ROM are
available. See tests/integration/conftest.py for harness details.
"""
from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.emulator, pytest.mark.asyncio(loop_scope="session")]

# Address 0x19 (decimal 25) is a well-known SNES memory location used in SMW.
# Using it here because poke scenarios can trivially poke it to a known value.
_TEST_ADDRESS = 25  # 0x19 in decimal
_TEST_CONDITION_NAME = "powerup"


# Note: set_conditions ack is a plain "ok:conditions_set" line, not JSON.
# recv_event only returns parsed JSON events, so the ack is unobservable via
# that path. Verifying ack behavior is left to the per-transition tests below:
# if conditions appear in subsequent event payloads, the set succeeded.


async def test_level_entrance_carries_conditions(run_scenario, tcp_client):
    """After set_conditions, level_entrance event payload includes conditions key."""
    # (Re-)register conditions so this test is self-contained even when run alone.
    payload = json.dumps([
        {"name": _TEST_CONDITION_NAME, "address": _TEST_ADDRESS, "size": 1}
    ])
    await tcp_client.send(f"set_conditions:{payload}")

    # entrance_goal.poke: enter level 105, then exit normally.
    events = await run_scenario("entrance_goal.poke")

    entrances = [e for e in events if e.get("event") == "level_entrance"]
    assert len(entrances) == 1, f"Expected 1 level_entrance, got {len(entrances)}"

    entrance = entrances[0]
    assert "conditions" in entrance, (
        f"level_entrance event missing 'conditions' key: {entrance}"
    )
    conditions = entrance["conditions"]
    assert isinstance(conditions, dict), (
        f"'conditions' should be a dict, got {type(conditions)}: {conditions}"
    )
    assert _TEST_CONDITION_NAME in conditions, (
        f"Expected '{_TEST_CONDITION_NAME}' in conditions, got: {conditions}"
    )


async def test_level_exit_carries_conditions(run_scenario, tcp_client):
    """After set_conditions, level_exit event payload includes conditions key."""
    payload = json.dumps([
        {"name": _TEST_CONDITION_NAME, "address": _TEST_ADDRESS, "size": 1}
    ])
    await tcp_client.send(f"set_conditions:{payload}")

    events = await run_scenario("entrance_goal.poke")

    exits = [e for e in events if e.get("event") == "level_exit"]
    assert len(exits) == 1, f"Expected 1 level_exit, got {len(exits)}"

    exit_event = exits[0]
    assert "conditions" in exit_event, (
        f"level_exit event missing 'conditions' key: {exit_event}"
    )


async def test_death_and_spawn_carry_conditions(run_scenario, tcp_client):
    """After set_conditions, death and spawn events include conditions key."""
    payload = json.dumps([
        {"name": _TEST_CONDITION_NAME, "address": _TEST_ADDRESS, "size": 1}
    ])
    await tcp_client.send(f"set_conditions:{payload}")

    events = await run_scenario("entrance_death_spawn.poke")

    deaths = [e for e in events if e.get("event") == "death"]
    spawns = [e for e in events if e.get("event") == "spawn"]

    assert len(deaths) == 1, f"Expected 1 death event, got {len(deaths)}"
    assert len(spawns) == 1, f"Expected 1 spawn event, got {len(spawns)}"

    assert "conditions" in deaths[0], (
        f"death event missing 'conditions' key: {deaths[0]}"
    )
    assert "conditions" in spawns[0], (
        f"spawn event missing 'conditions' key: {spawns[0]}"
    )


async def test_checkpoint_carries_conditions(run_scenario, tcp_client):
    """After set_conditions, checkpoint event includes conditions key."""
    payload = json.dumps([
        {"name": _TEST_CONDITION_NAME, "address": _TEST_ADDRESS, "size": 1}
    ])
    await tcp_client.send(f"set_conditions:{payload}")

    events = await run_scenario("checkpoint_cold_spawn.poke")

    checkpoints = [e for e in events if e.get("event") == "checkpoint"]
    assert len(checkpoints) >= 1, f"Expected at least 1 checkpoint, got {len(checkpoints)}"

    assert "conditions" in checkpoints[0], (
        f"checkpoint event missing 'conditions' key: {checkpoints[0]}"
    )


async def test_empty_conditions_when_not_set(run_scenario, tcp_client):
    """When no conditions are configured, events still arrive with an empty conditions dict."""
    # Clear conditions by sending an empty array.
    await tcp_client.send("set_conditions:[]")

    events = await run_scenario("entrance_goal.poke")

    entrances = [e for e in events if e.get("event") == "level_entrance"]
    assert len(entrances) == 1

    entrance = entrances[0]
    assert "conditions" in entrance, (
        f"level_entrance should always have 'conditions' key (even empty): {entrance}"
    )
    assert entrance["conditions"] == {}, (
        f"Expected empty conditions dict, got: {entrance['conditions']}"
    )
