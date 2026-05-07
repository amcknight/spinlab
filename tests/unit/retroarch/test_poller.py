import asyncio
from typing import Iterator

import pytest

from spinlab.retroarch.events import Death, TransitionEvent
from spinlab.retroarch.poller import Poller, PollerDeps
from spinlab.retroarch.snapshot import MemorySnapshot


class _FakeClient:
    """Minimal NCIClient stand-in for poller tests."""

    def __init__(self) -> None:
        self.read_calls = 0


def _make_snapshots(seq: Iterator[MemorySnapshot]):
    """Wrap a snapshot iterator into a callable matching the deps signature."""

    def fn(_client) -> MemorySnapshot:
        return next(seq)

    return fn


def _snap(**ov) -> MemorySnapshot:
    base = dict(
        game_mode=0, level_num=0, room_num=0, level_start=0, player_anim=0,
        exit_mode=0, io_port=0, fanfare=0, boss_defeat=0, midway=0, cp_entrance=0,
    )
    base.update(ov)
    return MemorySnapshot(**base)


@pytest.mark.asyncio
async def test_poller_emits_death_event():
    """Poller fed a death sequence emits a Death event to the callback."""
    snapshots = iter([
        _snap(player_anim=0),  # frame 1
        _snap(player_anim=9),  # frame 2 -> death
        _snap(player_anim=9),  # frame 3 -> still dying, no event
    ])
    received: list[TransitionEvent] = []

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

    assert any(isinstance(e, Death) for e in received), f"got: {received}"


@pytest.mark.asyncio
async def test_poller_resync_clears_phantom_edges():
    """After mark_state_loaded(), the loaded-state snapshot becomes prev — no phantom Death.

    Real-world scenario: user loads a state where Mario is already dead. Without
    the resync hook, prev (pre-load, alive) → curr (post-load, dead) would fire
    a phantom Death. The resync replaces prev with the loaded state instead.
    """
    snapshots = iter([
        _snap(player_anim=9),  # post-load: state where Mario is dead
        _snap(player_anim=9),  # next frame: still dead, no edge
    ])
    received: list[TransitionEvent] = []

    deps = PollerDeps(
        client=_FakeClient(),
        read_snapshot=_make_snapshots(snapshots),
        on_event=received.append,
    )
    poller = Poller(deps, period_sec=0.001)
    task = asyncio.create_task(poller.run())
    poller.mark_state_loaded()
    await asyncio.sleep(0.05)
    poller.stop()
    await task

    assert not any(isinstance(e, Death) for e in received), f"got: {received}"
