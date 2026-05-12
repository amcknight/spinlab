"""The poller calls state_path_for(event) and stamps results onto events."""
import asyncio
from typing import Iterator

import pytest

from spinlab.protocol import (
    DeathEvent,
    LevelEntranceEvent,
    SpawnEvent,
)
from spinlab.retroarch.poller import Poller, PollerDeps
from spinlab.retroarch.snapshot import MemorySnapshot


class _FakeClient:
    pass


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
async def test_state_path_for_called_on_each_event():
    """The resolver runs for every emitted event and its return is stamped."""
    snapshots = iter([
        _snap(level_num=5),  # seed
        _snap(level_num=5, level_start=1),  # entrance
    ])
    received: list = []
    resolver_calls: list = []

    def resolver(ev) -> str:
        resolver_calls.append(ev)
        if isinstance(ev, LevelEntranceEvent):
            return "/states/seg-1.state"
        return ""

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
        state_path_for=resolver,
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    entrances = [e for e in received if isinstance(e, LevelEntranceEvent)]
    assert len(entrances) == 1
    assert entrances[0].state_path == "/states/seg-1.state"
    assert resolver_calls, "resolver was never invoked"


@pytest.mark.asyncio
async def test_resolver_returning_empty_keeps_existing_state_path():
    """When the resolver returns '', the event's state_path stays as detector emitted it (default '')."""
    snapshots = iter([
        _snap(player_anim=0),
        _snap(player_anim=9),
    ])
    received: list = []

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
        state_path_for=lambda ev: "",
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    deaths = [e for e in received if isinstance(e, DeathEvent)]
    assert len(deaths) == 1
    # Death has no state_path field; this asserts we didn't crash trying to set one.


@pytest.mark.asyncio
async def test_resolver_optional_default_none():
    """If state_path_for is None, the poller skips resolution entirely."""
    snapshots = iter([
        _snap(level_num=5),
        _snap(level_num=5, level_start=1),
    ])
    received: list = []

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
        # state_path_for omitted — defaults to None.
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    entrances = [e for e in received if isinstance(e, LevelEntranceEvent)]
    assert len(entrances) == 1
    assert entrances[0].state_path is None  # protocol default unchanged


def test_spawn_has_segment_id_field():
    """SpawnEvent must carry segment_id so cold-fill resolution can compute its path."""
    s = SpawnEvent(timestamp_ms=0, level_num=5, segment_id="seg-x")
    assert s.segment_id == "seg-x"
