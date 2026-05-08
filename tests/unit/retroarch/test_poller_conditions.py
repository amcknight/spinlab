"""Tests verifying the poller populates event.conditions from ConditionRegistry."""
import asyncio
from typing import Iterator

import pytest

from spinlab.condition_registry import ConditionRegistry
from spinlab.protocol import LevelEntranceEvent
from spinlab.retroarch.poller import Poller, PollerDeps
from spinlab.retroarch.snapshot import MemorySnapshot


class _FakeClient:
    """Stub NCIClient that returns scripted reads for ConditionRegistry's read_all path."""

    def __init__(self, ram_values: dict[int, int] | None = None) -> None:
        self.ram_values = ram_values or {}

    def read_ram(self, addr: int, size: int) -> bytes:
        v = self.ram_values.get(addr, 0)
        if size == 1:
            return bytes([v & 0xFF])
        return bytes([v & 0xFF, (v >> 8) & 0xFF])


def _snap(**ov) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(ov)
    return MemorySnapshot(**base)


def _make_snapshots(seq: Iterator[MemorySnapshot]):
    def fn(_client) -> MemorySnapshot:
        return next(seq)
    return fn


@pytest.mark.asyncio
async def test_poller_populates_event_conditions_from_registry():
    """When conditions_registry is set, emitted events have populated conditions."""
    reg = ConditionRegistry()
    reg.replace_with_read_specs([
        {"name": "powerup", "address": 0x19, "size": 1},
    ])
    snapshots = iter([
        _snap(level_num=5),
        _snap(level_num=5, level_start=1),  # entrance
    ])
    received: list = []

    deps = PollerDeps(
        client=_FakeClient(ram_values={0x19: 2}),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
        conditions_registry=reg,
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    entrances = [e for e in received if isinstance(e, LevelEntranceEvent)]
    assert len(entrances) == 1
    assert entrances[0].conditions == {"powerup": 2}


@pytest.mark.asyncio
async def test_poller_no_registry_leaves_conditions_empty():
    """conditions_registry=None (default) leaves events at conditions={}."""
    snapshots = iter([
        _snap(level_num=5),
        _snap(level_num=5, level_start=1),
    ])
    received: list = []

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    entrances = [e for e in received if isinstance(e, LevelEntranceEvent)]
    assert len(entrances) == 1
    assert entrances[0].conditions == {}
